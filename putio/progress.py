"""The transfer-progress protocol, written from Python instead of from bash.

``putioctl progress``, the curses console and ``putio-statusd`` all read these
files, and none of them is being changed in this phase — so the format here is
a transcription of ``progress_write`` and the file names are the ones
``progress_sweep``, ``on_exit`` and every reader already glob for::

    dl.<runpid>.<jobid>.meta   RUN_PID / PHASE / STARTED / SIZE / NAME /
                               REMOTE / DEST / LOG, one KEY=value per line
    dl.<runpid>.<jobid>.log    rclone's own --use-json-log output

**``<runpid>`` is bash's ``$$``, not this process's pid**, and that is
load-bearing in three places: readers call ``kill -0`` on it to tell a live
transfer from what a killed run left behind, ``progress_sweep`` uses it to
decide what to clear at startup, and the processor's ``on_exit`` trap removes
exactly ``dl.$$.*``.  Publishing under the interpreter's own pid would make
every bar vanish the instant the batch ended and leave the meta files behind
for the next run to sweep.  It arrives as ``RUN_PID`` on the environment; see
``PUTIO_PY_VARS``.

``<jobid>`` is opaque to every reader — they take field 2 and stop — so it is a
counter here rather than bash's ``sh -c 'echo $PPID'``.  That trick exists only
because bash 3.2 has no ``$BASHPID`` and every download subshell shares ``$$``;
it hands back a pid belonging to a process that has *already exited*, so two
jobs really can collide on one name.  Threads have no such problem and a
counter cannot repeat.

The meta file is written whole to ``.tmp`` and renamed, because a reader
polling every couple of seconds would otherwise catch it half-written and draw
a transfer with no size.
"""

from __future__ import annotations

import errno
import json
import os
import re
import threading
import time
from typing import List, Optional

__all__ = ["ProgressPublisher", "Job", "NullJob", "error_tail"]

#: Bash: `grep -v '"stats"' | tail -n 8`.  The tail is applied to the filtered
#: lines, before anything is parsed.
_TAIL_LINES = 8
#: ...then `tail -n 2` of the distinct messages.
_KEEP_MESSAGES = 2
#: ...then `cut -c1-300`.
_MAX_CHARS = 300

_SPACES = re.compile(r"  +")


def error_tail(log_path: str) -> str:
    """rclone's own last words, dug back out of the JSON log.

    ``--log-file`` takes rclone's whole output, which leaves its stderr empty —
    so without this a failed download logs "Failed to download X after 3
    attempts" and nothing at all about why.  Bash reaches the same string
    through ``grep -v | tail | jq | awk | tail | tr | sed | cut``; this is the
    same pipeline, in the same order, with the same quirks:

    * the messages are deduplicated because rclone repeats itself once per
      attempt, and only the last two survive;
    * the result is capped, because it rides along on one log line — the full
      transcript is in the progress log until the job ends;
    * an empty answer contributes nothing, so the caller can concatenate it.

    Returns ``" — <messages>"`` or ``""``.  Never raises: this runs on the
    failure path, where a second failure would replace the only diagnosis.
    """
    if not log_path:
        return ""
    try:
        if os.path.getsize(log_path) <= 0:
            return ""
        with open(log_path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""

    candidates = [line for line in lines if '"stats"' not in line][-_TAIL_LINES:]

    seen = set()
    messages = []  # type: List[str]
    for line in candidates:
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        message = payload.get("msg") or ""
        if not isinstance(message, str) or not message.strip():
            continue
        if message in seen:
            continue
        seen.add(message)
        messages.append(message)

    messages = messages[-_KEEP_MESSAGES:]
    if not messages:
        return ""

    # `tr '\n' ' '` leaves a trailing space, which the sed then strips; doing
    # it in one step here reaches the same bytes.
    joined = _SPACES.sub(" ", " ".join(messages)).rstrip(" ")
    joined = joined[:_MAX_CHARS]
    if not joined:
        return ""
    return " — " + joined


class NullJob:
    """What a job looks like with ``PROGRESS_STATE_DIR`` switched off.

    Empty is a real setting (see ``putio.config``), and the download path asks
    a job for its log path to hand to rclone — so "publish nothing" has to be
    an object rather than ``None`` at every call site.
    """

    meta_path = ""
    log_path = ""

    def phase(self, name: str, dest: Optional[str] = None) -> None:
        return None

    def error_tail(self) -> str:
        return ""

    def clear(self) -> None:
        return None

    def __enter__(self) -> "NullJob":
        return self

    def __exit__(self, *exc) -> bool:
        self.clear()
        return False


class Job:
    """One in-flight transfer's pair of files."""

    def __init__(
        self,
        state_dir: str,
        run_pid: str,
        job_id: str,
        remote: str,
        size: str,
        dest: str,
        started: Optional[int] = None,
    ) -> None:
        self.meta_path = os.path.join(state_dir, "dl.{0}.{1}.meta".format(run_pid, job_id))
        self.log_path = os.path.join(state_dir, "dl.{0}.{1}.log".format(run_pid, job_id))
        self._run_pid = run_pid
        self._remote = remote
        self._size = size
        self._dest = dest
        self._phase = "download"
        self._started = str(int(time.time()) if started is None else started)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _render(self) -> str:
        # Order and spelling are ``progress_write``'s.  ``NAME`` is the
        # basename because that is what a bar is labelled with.
        return (
            "RUN_PID={0}\n"
            "PHASE={1}\n"
            "STARTED={2}\n"
            "SIZE={3}\n"
            "NAME={4}\n"
            "REMOTE={5}\n"
            "DEST={6}\n"
            "LOG={7}\n"
        ).format(
            self._run_pid,
            self._phase,
            self._started,
            self._size,
            os.path.basename(self._remote),
            self._remote,
            self._dest,
            self.log_path,
        )

    def write(self) -> None:
        """Publish the current state.  Never raises — a progress file that
        cannot be written must not take a working download with it."""
        with self._lock:
            tmp = self.meta_path + ".tmp"
            try:
                with open(
                    tmp, "w", encoding="utf-8", errors="surrogateescape"
                ) as handle:
                    handle.write(self._render())
                os.replace(tmp, self.meta_path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def phase(self, name: str, dest: Optional[str] = None) -> None:
        """Move to a phase rclone is not driving.

        The staged copy onto the SMB share is a plain copy that reports
        nothing, so readers fall back to measuring the destination file — which
        only works if they are told to.
        """
        with self._lock:
            self._phase = name
            if dest is not None:
                self._dest = dest
        self.write()

    def error_tail(self) -> str:
        return error_tail(self.log_path)

    def clear(self) -> None:
        for path in (self.meta_path, self.meta_path + ".tmp", self.log_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc) -> bool:
        # The bash equivalent is the `download_file` wrapper, which exists for
        # exactly this: `download_file_run` returns from a dozen places and a
        # missed one leaves a finished transfer sitting at 90% in every reader
        # until the run exits.
        self.clear()
        return False


class ProgressPublisher:
    """Hands out :class:`Job` objects for one run."""

    def __init__(self, state_dir: Optional[str], run_pid: str) -> None:
        self.state_dir = state_dir or ""
        self.run_pid = str(run_pid or os.getpid())
        self._next = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.state_dir)

    def begin(self, remote: str, size: str, dest: str):
        """Start publishing for one transfer.

        Written before the first byte moves, so a transfer appears in every
        reader immediately rather than at the first stats tick.
        """
        if not self.enabled:
            return NullJob()

        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                return NullJob()

        with self._lock:
            self._next += 1
            job_id = str(self._next)

        job = Job(self.state_dir, self.run_pid, job_id, remote, str(size), dest)
        try:
            # Truncate/create the rclone log up front: bash does the same, and
            # a --log-file it cannot open is the one flag that would fail the
            # transfer rather than just the bar.
            with open(job.log_path, "w"):
                pass
        except OSError:
            return NullJob()
        job.write()
        return job

    def clear_all(self) -> None:
        """Remove every file this run published.

        Not used on the ordinary path — each job clears its own — but a caller
        that is about to abandon the batch has no other way to leave the
        readers clean.
        """
        if not self.enabled:
            return
        prefix = "dl.{0}.".format(self.run_pid)
        try:
            entries = os.listdir(self.state_dir)
        except OSError:
            return
        for entry in entries:
            if not entry.startswith(prefix):
                continue
            try:
                os.unlink(os.path.join(self.state_dir, entry))
            except OSError:
                pass
