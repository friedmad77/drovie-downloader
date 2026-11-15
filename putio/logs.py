"""The processor's log file — the format, the level gate, and the rotation.

Phase 5 makes Python the entry point, so this is the first module that writes
to the log rather than handing lines back for bash to write.  Everything here
is a transcription of ``log``, ``get_log_level_num`` and ``rotate_log``, and
the reason it is a transcription rather than a tidy-up is that **the line
format is an invariant**:

* ``rotate_log`` (below, and still in the shim's ``log``) compares the
  ``YYYY-MM-DD HH:MM:SS`` stamps as *strings*.  That works only because the
  format is zero-padded and big-endian, which makes lexical order
  chronological order.  A change to the stamp silently changes what rotation
  keeps.
* ``putioctl status`` greps the log for ``Script completed successfully``.
* The curses console filters on ``[LEVEL]``, and ``putioctl logs --errors``
  greps for it.
* The shim still logs from ``--mount-gate``, in bash, into the same file.  Two
  writers, one format; they have to agree.

**Bytes, not text, on the way out and on the way in.**  rclone's stderr lands
in this file verbatim and put.io produces names that are not valid UTF-8, so
rotation reads and writes binary and never decodes.  A ``str`` message is
encoded with ``surrogateescape`` so a name that round-tripped through
``os.fsdecode`` comes back out as the bytes it arrived as.

**One open-append-close per line, deliberately.**  It mirrors what ``tee -a``
did per call, and it is what makes rotation safe to run mid-run: a held-open
handle would keep writing into the unlinked inode after the file was replaced,
and the lines would vanish.  It is also far cheaper than what it replaces —
bash forked a ``date`` *and* a ``tee`` for every line.
"""

from __future__ import annotations

import os
import random
import re
import sys
import threading
import time
from typing import Optional

__all__ = ["LEVELS", "Logger", "format_duration", "rotate"]

#: ERROR=1 … DEBUG=4, and a line is written when its level is <= the
#: configured one.  ``get_log_level_num`` maps anything unrecognised to INFO's
#: 3, for the level *and* for the threshold, and so does this.
LEVELS = {"ERROR": 1, "WARN": 2, "INFO": 3, "DEBUG": 4}
_DEFAULT_LEVEL = 3

#: ``[2026-08-23 14:02:11]`` — anchored, because an untimestamped line
#: (raw rclone stderr, a multi-line continuation) inherits the retention
#: decision of the timestamped line above it.
_STAMPED = re.compile(rb"^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]")

#: Where in a stamped line the comparable text sits: ``substr($0, 2, 19)``.
_STAMP_SLICE = slice(1, 20)

_RETENTION_SECONDS = 86400

#: ``log`` rotates on roughly one call in a hundred.  Kept, because without it
#: a single long run — hours of transfers at multiple lines per file — grows
#: the log with no trim until the next startup.  Reproduced rather than
#: improved: a time-based trigger would be better and is not this phase's
#: change to make.
_ROTATE_ODDS = 100

#: Serialises rotation within a process.  Across processes there is still a
#: race — the shim's own bash ``log`` writes into the same file from
#: ``--mount-gate`` — but that one is bash's and predates the port; this closes
#: the one the thread pool introduced.
_ROTATE_LOCK = threading.Lock()


def level_number(level: str) -> int:
    """``get_log_level_num`` — unrecognised names read as INFO, both ways."""
    return LEVELS.get(level, _DEFAULT_LEVEL)


def format_duration(seconds: int) -> str:
    """``format_duration`` — the run summary's elapsed time."""
    if seconds < 60:
        return "{0}s".format(seconds)
    if seconds < 3600:
        return "{0}m{1:02d}s".format(seconds // 60, seconds % 60)
    return "{0}h{1:02d}m".format(seconds // 3600, (seconds % 3600) // 60)


def _encode(text: str) -> bytes:
    """UTF-8, but never raising on a name that was never UTF-8 to begin with."""
    return text.encode("utf-8", "surrogateescape")


class Logger:
    """``log`` — level gate, timestamp, tee to stdout and the log file.

    ``rotate_on_write`` exists for the tests and for any caller that wants a
    logger with no side effects beyond the two writes; production leaves it on.
    """

    def __init__(
        self,
        log_file: str = "",
        level: str = "INFO",
        stream=None,
        rotate_on_write: bool = True,
        _random=None,
    ) -> None:
        self.log_file = log_file
        self.threshold = level_number((level or "INFO").upper())
        self._stream = stream if stream is not None else sys.stdout
        self._rotate_on_write = rotate_on_write
        self._random = _random if _random is not None else random.randrange

    def __call__(self, level: str, message: str) -> None:
        level = (level or "INFO").upper()
        if level not in LEVELS:
            # Bash's `log` only shifts the level off the front when it matches
            # DEBUG|INFO|WARN|ERROR; anything else was part of the message.
            # Nothing in this package calls it that way, so treat it as INFO
            # rather than silently making the word disappear.
            message = "{0} {1}".format(level, message)
            level = "INFO"
        if level_number(level) > self.threshold:
            return

        line = "[{0}] [{1}] {2}".format(
            time.strftime("%Y-%m-%d %H:%M:%S"), level, message
        )
        self._emit(line)

        if self._rotate_on_write and self.log_file and self._random(_ROTATE_ODDS) == 0:
            rotate(self.log_file)

    # ------------------------------------------------------------------
    def _emit(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            # A closed or full stdout must not take the run down; the log file
            # is the record that matters and it is written below regardless.
            pass
        if not self.log_file:
            return
        try:
            with open(self.log_file, "ab") as handle:
                handle.write(_encode(line) + b"\n")
        except OSError:
            # Same reasoning as above, and the more likely case: LOG_FILE on a
            # share that just dropped.  The run carries on; the transfers are
            # what matter and they report their own failures.
            pass


def rotate(log_file: str, now: Optional[float] = None) -> bool:
    """``rotate_log`` — keep the last 24 hours, comparing stamps as strings.

    Returns True if the file was rewritten.

    The string compare is the whole performance story of this function and is
    written down in CLAUDE.md: parsing each line back into an epoch cost a
    ``sed`` and up to two ``date`` processes per line, which measured **36
    seconds of every idle run** on a log of ~1600 lines.  Do not reintroduce
    per-line work here that is more expensive than a slice and a comparison.

    Failure modes are all "keep the log":

    * no such file — nothing to do
    * an unreadable clock — the cutoff is empty, which compares less than
      every stamp, so everything is kept
    * an unwritable directory — the original is left exactly as it was
    """
    if not log_file or not os.path.isfile(log_file):
        return False

    if now is None:
        now = time.time()
    try:
        cutoff = _encode(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - _RETENTION_SECONDS))
        )
    except (OSError, OverflowError, ValueError):
        # An empty cutoff compares <= every stamp, so the whole log is kept.
        # That is the right way for this to fail: rotation is a tidy-up, and
        # losing history to a broken clock is worse than keeping too much.
        cutoff = b""

    # The temp name carries the *thread* as well as the process, and the
    # rewrite is taken under a lock.  Both are needed because this Logger is
    # handed to Batch, Downloader, RcloneClient and Retrier, all of which run
    # on ThreadPoolExecutor workers — so two threads can hit the 1-in-100
    # rotation trigger at once.  Sharing one temp path let them interleave
    # their writes and then both rename over the log, and the log is what
    # `putioctl status` greps and the console renders.
    directory = os.path.dirname(log_file) or "."
    temp_path = os.path.join(directory, ".{0}.rotate.{1}.{2}".format(
        os.path.basename(log_file), os.getpid(), threading.get_ident()
    ))

    with _ROTATE_LOCK:
        return _rotate_locked(log_file, cutoff, temp_path)



def _rotate_locked(log_file: str, cutoff: bytes, temp_path: str) -> bool:
    try:
        kept_any = False
        with open(log_file, "rb") as source, open(temp_path, "wb") as target:
            keeping = False
            for line in source:
                if _STAMPED.match(line):
                    keeping = line[_STAMP_SLICE] >= cutoff
                elif not keeping:
                    # Untimestamped lines before the first kept stamp are
                    # dropped, so ancient rclone output cannot ride along
                    # forever on a continuation.
                    continue
                if keeping:
                    target.write(line)
                    kept_any = True
    except OSError:
        _unlink(temp_path)
        return False

    try:
        if kept_any:
            # Beside the log rather than in /tmp, which is what bash used: this
            # makes the swap a same-filesystem rename, so a reader never sees a
            # half-written log and a full /tmp cannot lose the file.
            os.replace(temp_path, log_file)
        else:
            # An empty result truncates in place rather than replacing, exactly
            # as `> "$LOG_FILE"` did.
            with open(log_file, "wb"):
                pass
            _unlink(temp_path)
    except OSError:
        _unlink(temp_path)
        return False
    return True


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
