"""The run lock — one sync at a time, and the pid so ``putioctl`` can find it.

A transcription of ``acquire_lock`` and the block that follows it.  Two things
about it are contract rather than implementation:

* **The lockfile holds the pid**, and ``putioctl status`` reads it to report
  the running run, ``putioctl stop`` walks ``pgrep -P`` down from it to find
  the rclone children, and ``putioctl deploy`` refuses while it names a live
  process.  ``putio-statusd`` reads it through ``putioctl status --json``.
* **It is acquired atomically.**  Bash used ``set -o noclobber`` for exactly
  the reason ``O_EXCL`` is used here: two cron firings that raced between a
  ``test -e`` and a ``touch`` would both have slipped through.

The pid written is this process's, and after phase 5 that is *also* the pid
cron started: the shim ``exec``s into Python, so the process image is replaced
and the pid is kept.  Nothing downstream had to change for that, which is the
main reason the shim execs rather than forking.

Breaking a stale lock is racy by nature and stays exactly as racy as it was:
between noticing the holder is dead and re-acquiring, another run can take it,
and the second acquire simply fails.  That is the safe direction.
"""

from __future__ import annotations

import errno
import os
from typing import Optional

__all__ = ["LockError", "RunLock"]


class LockError(Exception):
    """The lock could not be taken, and the run must not proceed."""

    def __init__(self, message: str, holder: Optional[int] = None) -> None:
        super().__init__(message)
        self.holder = holder


def _is_live(pid: int) -> bool:
    """``kill -0`` — is there a process with this pid we could signal?

    A pid the kernel has reissued reads as live here, which is the same answer
    bash got.  Unlike the progress and staging sweeps there is no boot-time
    guard to add: the lockfile is in /tmp, which macOS clears at boot, so a
    lockfile that survived a reboot is not a case that arises.
    """
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # EPERM means it exists and belongs to somebody else — still live.
        return exc.errno == errno.EPERM
    return True


class RunLock:
    """Hold the processor's lockfile for the length of a run."""

    def __init__(self, path: str, log=None) -> None:
        self.path = path
        self._log = log if log is not None else (lambda level, message: None)
        self._held = False

    # ------------------------------------------------------------------
    def _acquire_once(self) -> bool:
        """``(set -o noclobber; echo $$ > "$LOCKFILE")``."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
        try:
            os.write(fd, "{0}\n".format(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        self._held = True
        return True

    def holder(self) -> Optional[int]:
        """The pid in the lockfile, or None if it is missing or unreadable."""
        try:
            with open(self.path, "r") as handle:
                text = handle.read().strip()
        except OSError:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def acquire(self) -> None:
        """Take the lock, breaking a stale one.  Raises :class:`LockError`."""
        if self._acquire_once():
            return

        existing = self.holder()
        if existing is not None and _is_live(existing):
            raise LockError(
                "A sync is already running (PID {0}).".format(existing),
                holder=existing,
            )

        self._log(
            "WARN",
            "[LOCK] Stale lock found (PID '{0}' not running), breaking".format(
                existing if existing is not None else "unknown"
            ),
        )
        try:
            os.unlink(self.path)
        except OSError:
            pass
        if not self._acquire_once():
            # Another run took it in the gap.  Bash said the same thing and
            # gave up the same way; retrying would be how two syncs end up
            # running at once.
            raise LockError("[LOCK] Failed to acquire lock after breaking stale lock. Exiting.")

    def release(self) -> None:
        """Drop the lock, but only while it is still ours.

        The pid check is what ``putioctl``'s ``release_lock_of`` does and for
        the same reason: between this run dying and its cleanup running, cron
        can fire and a fresh run can take the lock.  Deleting *that* would let
        a later tick start a second concurrent sync — the exact thing the lock
        exists to prevent.
        """
        if not self._held:
            return
        if self.holder() == os.getpid():
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self._held = False

    # ------------------------------------------------------------------
    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False
