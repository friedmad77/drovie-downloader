"""The per-file share check, with recovery left where it belongs.

The plan's phase 6 says the SMB recovery path is the one place where stopping
is better than finishing: it is ``osascript``, Finder, ``mount_smbfs``, TCP
probes, cross-run backoff files and dialog suppression, and Python would still
be shelling out to all of it, just from further away.  So none of that moves
here.  What moves is only the *decision* the download loop makes with it.

``process_single_file`` re-checks the share before every file — the NAS can
drop between two downloads, and without the check the rest of the queue blasts
through with every move failing into ``UNMATCHED_DIR`` on a stale path.  The
check itself is ``ensure_mount_alive``, which the processor exposes as a mode:

    putio_download_and_delete.sh --mount-gate

That re-enters the script, which sources the config and defines the functions,
runs ``ensure_mount_alive`` and exits with its status.  It costs one bash start
(~70ms measured) per file in a loop whose items take *minutes*, so it is not
the per-item subprocess the batching rule forbids — that rule is about items
that are cheaper than the fork.

Its own log lines go straight into ``LOG_FILE`` through ``log``, with correct
timestamps, so this captures its stdout only to keep the copy ``tee`` writes
there from being mistaken for a record.

**The one piece of state that does move is the "dead" flag.**  Bash keeps it in
``$MEDIA_MOUNT_STATE_DIR/mount_dead.$$`` because the download jobs are
subshells and a variable set in one is invisible to the next.  There are no
subshells any more, so it is an attribute — which also means it cannot be left
behind by a run that dies before its EXIT trap.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .proc import run

__all__ = ["ALIVE", "BUSY", "DEAD", "MountGate"]

#: The share is usable.
ALIVE = 0
#: A remount is running (started by this run or another); try again later.
#: Deliberately *not* failure — the holder may yet succeed, and declaring the
#: share dead here would abort a queue seconds before it recovered.
BUSY = 2
#: The share is gone and could not be brought back.
DEAD = 1


class MountGate:
    """``ensure_mount_alive`` for one run, asked once per file."""

    def __init__(
        self,
        config,
        script_path: str,
        log: Callable[[str, str], None],
        runner: Optional[Callable[[], int]] = None,
    ) -> None:
        self._config = config
        self._script_path = script_path
        self._log = log
        self._runner = runner
        self._dead = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """False when ``MEDIA_MOUNT_POINT`` is unset — the feature is off, and
        bash's ``is_mount_alive`` returns success immediately in that case."""
        return bool(self._config.media_mount_point)

    @property
    def dead(self) -> bool:
        with self._lock:
            return self._dead

    def mark_dead(self) -> None:
        with self._lock:
            self._dead = True

    def probe(self) -> int:
        """Ask bash, and return its status unchanged: ALIVE, BUSY or DEAD.

        Public because the run's startup preflight needs the *status* rather
        than :meth:`check`'s verdict — it has its own rule (if the share is
        still in the mount table, a failing health check is not grounds to
        refuse the run) and it must not ``mark_dead`` on the way past, or the
        first file would be skipped over a preflight the run decided to
        tolerate.
        """
        if self._runner is not None:
            return self._runner()
        if not self._script_path:
            # Nothing to ask.  Treat the share as usable rather than blocking
            # the queue on a missing helper: the moves themselves still fail
            # loudly if it really is gone, which is where the run was before
            # the gate existed at all.
            return ALIVE
        result = run([self._script_path, "--mount-gate"])
        return result.returncode

    def check(self, file_name: str) -> bool:
        """True if ``file_name`` may be processed now.

        Logs the same three lines ``process_single_file`` does, so a run reads
        the same as it did before the port.
        """
        if not self.enabled:
            return True

        if self.dead:
            self._log(
                "ERROR",
                "[MOUNT] Skipping {0} — share already declared unavailable this run".format(
                    file_name
                ),
            )
            return False

        status = self.probe()
        if status == ALIVE:
            return True

        if status == BUSY:
            self._log(
                "WARN",
                "[MOUNT] Deferring {0} — a remount is still in progress".format(
                    file_name
                ),
            )
            return False

        self._log(
            "ERROR",
            "[MOUNT] Aborting {0} — {1} is unavailable".format(
                file_name, self._config.media_mount_point
            ),
        )
        self.mark_dead()
        return False
