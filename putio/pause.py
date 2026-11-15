"""The pause gate — ``putioctl pause`` holds off scheduled runs.

A transcription of the block above the lock in the processor.  Three details
in it are contract:

* **The file format.**  ``putioctl pause`` writes ``PAUSED_UNTIL`` and
  ``PAUSED_REASON``; ``putioctl status``, the console and ``putio-statusd``
  all read the same file.  It is parsed here exactly as bash parsed it,
  including which of the two has its quotes stripped how.
* **Exit 0 when paused.**  cron mails failures, and a run skipped on purpose
  is not one.
* **It is checked before the lock**, so a paused tick leaves no lockfile and
  no trace at all.

A timed pause clears itself here rather than needing anyone to run
``putioctl resume``, and the skip is logged **once per pause, not once per
tick** — rotation only runs on runs that proceed, so a line every five minutes
would grow the log for as long as the pause lasted with nothing ever trimming
it.  The state stays visible through ``putioctl status``.

``PUTIO_IGNORE_PAUSE=1`` bypasses the gate for one run, which is what
``putioctl sync --force`` sets.
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

__all__ = ["PauseState", "read_pause", "check_pause"]


class PauseState:
    """What the pause file says, already interpreted."""

    __slots__ = ("until", "reason", "expired")

    def __init__(self, until: Optional[int], reason: str, expired: bool) -> None:
        #: Epoch second the pause lapses, or None for an indefinite pause.
        self.until = until
        self.reason = reason
        self.expired = expired

    def describe(self) -> str:
        """``until 2026-08-23 14:02:11`` or ``indefinitely``, as bash spelled it."""
        if self.until is None:
            return "indefinitely"
        try:
            return "until " + time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.until)
            )
        except (OSError, OverflowError, ValueError):
            # `date -r` failing fell back to printing the raw epoch.
            return "until {0}".format(self.until)


def _field(lines, prefix: str) -> str:
    """``sed -n 's/^NAME=//p' | head -1`` — the first line that starts with it."""
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def read_pause(pause_file: str) -> Optional[PauseState]:
    """Parse the pause file, or None if there is no pause in force.

    The two fields are unquoted differently, and that asymmetry is bash's:
    ``PAUSED_UNTIL`` went through ``tr -d '"'``, which strips *every* quote
    anywhere in the value, while ``PAUSED_REASON`` went through
    ``sed 's/^"//; s/"$//'``, which strips only one at each end.  A reason
    containing a quote keeps it; an until containing one would not.
    """
    if not pause_file or not os.path.isfile(pause_file):
        return None
    try:
        with open(pause_file, "r", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        # Unreadable is not paused: a pause nobody can read is a pause nobody
        # can lift, and the sync stopping forever is the worse failure.
        return None

    raw_until = _field(lines, "PAUSED_UNTIL=").replace('"', "")
    reason = _field(lines, "PAUSED_REASON=")
    if reason.startswith('"'):
        reason = reason[1:]
    if reason.endswith('"'):
        reason = reason[:-1]

    until: Optional[int] = None
    # `[[ "$pause_until" =~ ^[0-9]+$ ]] && [ "$pause_until" -gt 0 ]` — anything
    # else, including a negative or a word, means an indefinite pause.
    if raw_until.isdigit():
        value = int(raw_until)
        if value > 0:
            until = value

    expired = until is not None and time.time() >= until
    return PauseState(until, reason, expired)


def _last_line(path: str) -> str:
    """``tail -n 1``, without reading a large log into memory."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            # 8 KiB is comfortably more than any line this project writes, and
            # a short read at the head of a small file is handled by max(0, …).
            handle.seek(max(0, size - 8192))
            tail = handle.read()
    except OSError:
        return ""
    lines = tail.splitlines()
    if not lines:
        return ""
    return lines[-1].decode("utf-8", "surrogateescape")


def check_pause(pause_file: str, log_file: str, log, env=None) -> bool:
    """Should this run stop here?  True means "exit 0, we are paused".

    Clears an expired pause as a side effect, which is what makes a timed
    pause resume on its own.
    """
    if env is None:
        env = os.environ
    if env.get("PUTIO_IGNORE_PAUSE", "0") == "1":
        return False

    state = read_pause(pause_file)
    if state is None:
        return False

    if state.expired:
        try:
            os.unlink(pause_file)
        except OSError:
            pass
        log("INFO", "[PAUSE] Pause expired — resuming scheduled runs")
        return False

    if "[PAUSE] Sync is paused" not in _last_line(log_file):
        log(
            "INFO",
            "[PAUSE] Sync is paused {0}{1} — skipping run".format(
                state.describe(),
                " ({0})".format(state.reason) if state.reason else "",
            ),
        )
    return True
