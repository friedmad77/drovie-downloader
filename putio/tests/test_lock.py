"""The run lock — atomic acquisition, and the pid ``putioctl`` reads out of it.

The lockfile is a contract with three outside readers (``putioctl status``,
``putioctl stop``, ``putio-statusd`` through the first), so what is asserted
here is the *file*: that it exists, that it holds this process's pid, and that
a second acquisition cannot succeed while it does.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from putio.lock import LockError, RunLock


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-lock-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "run.lock")
        self.logged = []

    def lock(self):
        return RunLock(self.path, lambda level, message: self.logged.append((level, message)))

    def contents(self):
        with open(self.path) as handle:
            return handle.read()


class AcquireTest(Sandbox):
    def test_it_writes_this_process_s_pid(self):
        """The number ``putioctl stop`` walks ``pgrep -P`` down from.  Since
        phase 5 the shim execs, so this is also the pid cron started."""
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(self.contents().strip(), str(os.getpid()))

    def test_a_second_acquisition_is_refused_while_the_holder_lives(self):
        first = self.lock()
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(LockError) as caught:
            self.lock().acquire()
        self.assertEqual(caught.exception.holder, os.getpid())
        self.assertIn("already running", str(caught.exception))

    def test_it_is_atomic(self):
        """``O_EXCL``, which is what ``set -o noclobber`` was for: two cron
        firings racing between a ``test -e`` and a ``touch`` would both have
        slipped through."""
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        # A second create with the same flags must fail rather than truncate.
        with self.assertRaises(OSError):
            os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    def test_a_stale_lock_is_broken(self):
        # Pid 1 exists; a pid no process has is what a crashed run leaves.
        with open(self.path, "w") as handle:
            handle.write("2147483646\n")
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(self.contents().strip(), str(os.getpid()))
        self.assertEqual(self.logged[0][0], "WARN")
        self.assertIn("Stale lock found", self.logged[0][1])

    def test_a_lockfile_with_no_pid_in_it_is_stale(self):
        with open(self.path, "w") as handle:
            handle.write("")
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertIn("'unknown'", self.logged[0][1])

    def test_a_lockfile_holding_junk_is_stale(self):
        with open(self.path, "w") as handle:
            handle.write("not-a-pid\n")
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(self.contents().strip(), str(os.getpid()))


class ReleaseTest(Sandbox):
    def test_release_removes_it(self):
        lock = self.lock()
        lock.acquire()
        lock.release()
        self.assertFalse(os.path.exists(self.path))

    def test_release_leaves_a_lock_a_newer_run_has_taken(self):
        """``putioctl``'s ``release_lock_of`` rule.  Between this run dying and
        its cleanup running, cron can fire and a fresh run can take the lock —
        deleting that would let a later tick start a second concurrent sync,
        which is the exact thing the lock exists to prevent.
        """
        lock = self.lock()
        lock.acquire()
        with open(self.path, "w") as handle:
            handle.write("999999\n")
        lock.release()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.contents().strip(), "999999")

    def test_releasing_a_lock_never_held_does_nothing(self):
        with open(self.path, "w") as handle:
            handle.write("{0}\n".format(os.getpid()))
        self.lock().release()
        self.assertTrue(os.path.exists(self.path))

    def test_it_works_as_a_context_manager(self):
        with self.lock():
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
