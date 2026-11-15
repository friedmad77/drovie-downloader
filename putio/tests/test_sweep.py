"""The startup sweeps, and the boot-time guard that makes two of them correct.

Every sweep here exists because a *killed* run never reaches its cleanup, so
the tests are all "what did the last run leave, and is it gone now".

The boot-time guard gets the most attention because it is the one that is not
obvious: a pid only identifies a process within its own boot, and after a
restart the kernel reissues the same numbers.  Without the guard, ``kill -0``
reports a recycled pid as alive and the entry is skipped by its sweep
**forever** — which for staging means a full-size media file parked in exactly
the space the disk check measures.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from putio import sweep


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-sweep-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logged = []
        # Boot time is cached at module level; pin it and put it back.
        self.addCleanup(setattr, sweep, "_cached_boot", None)

    def log(self, level, message):
        self.logged.append((level, message))

    def set_boot(self, epoch):
        sweep._cached_boot = epoch

    def touch(self, path, mtime=None):
        with open(path, "w") as handle:
            handle.write("x")
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def messages(self):
        return [message for _level, message in self.logged]


DEAD_PID = "2147483646"  # no process has this


class BootEpochTest(unittest.TestCase):
    def test_it_reads_a_plausible_boot_time(self):
        sweep._cached_boot = None
        value = sweep.boot_epoch(refresh=True)
        self.assertGreater(value, 1_000_000_000)
        self.assertLess(value, time.time() + 1)

    def test_it_is_cached(self):
        """Bash resolves this into a variable rather than a function for a
        reason: a ``$(boot_epoch)`` call site runs in a subshell, so a cache
        assigned inside never reached the parent and both sweeps spawned a
        ``sysctl`` and a ``sed`` per entry."""
        sweep._cached_boot = 12345
        self.assertEqual(sweep.boot_epoch(), 12345)


class OwnerIsLiveTest(Sandbox):
    def test_a_live_pid_touched_since_boot_owns_it(self):
        self.set_boot(int(time.time()) - 3600)
        path = os.path.join(self.dir, "entry")
        self.touch(path)
        self.assertTrue(sweep.owner_is_live(path, str(os.getpid())))

    def test_a_dead_pid_owns_nothing(self):
        self.set_boot(int(time.time()) - 3600)
        path = os.path.join(self.dir, "entry")
        self.touch(path)
        self.assertFalse(sweep.owner_is_live(path, DEAD_PID))

    def test_a_live_pid_on_a_pre_boot_entry_owns_nothing(self):
        """The recycled-pid case, which is the whole point of the guard."""
        now = int(time.time())
        self.set_boot(now - 60)
        path = os.path.join(self.dir, "entry")
        self.touch(path, mtime=now - 3600)
        self.assertFalse(sweep.owner_is_live(path, str(os.getpid())))

    def test_a_non_numeric_pid_owns_nothing(self):
        path = os.path.join(self.dir, "entry")
        self.touch(path)
        self.assertFalse(sweep.owner_is_live(path, ""))
        self.assertFalse(sweep.owner_is_live(path, "abc"))

    def test_an_unknown_boot_time_falls_back_to_the_pid_alone(self):
        """0 puts every mtime "since boot", so the guard only ever adds a
        reason to sweep — never a reason to keep."""
        self.set_boot(0)
        path = os.path.join(self.dir, "entry")
        self.touch(path, mtime=1)
        self.assertTrue(sweep.owner_is_live(path, str(os.getpid())))


class ProgressSweepTest(Sandbox):
    def test_it_drops_what_a_dead_run_left_and_keeps_a_live_one(self):
        self.set_boot(int(time.time()) - 3600)
        self.touch(os.path.join(self.dir, "dl.{0}.1.meta".format(DEAD_PID)))
        self.touch(os.path.join(self.dir, "dl.{0}.1.log".format(DEAD_PID)))
        mine = "dl.{0}.1.meta".format(os.getpid())
        self.touch(os.path.join(self.dir, mine))
        self.assertEqual(sweep.sweep_progress(self.dir), 2)
        self.assertEqual(os.listdir(self.dir), [mine])

    def test_it_ignores_files_that_are_not_ours(self):
        self.set_boot(int(time.time()) - 3600)
        self.touch(os.path.join(self.dir, "something-else"))
        self.assertEqual(sweep.sweep_progress(self.dir), 0)
        self.assertEqual(os.listdir(self.dir), ["something-else"])

    def test_progress_disabled_sweeps_nothing(self):
        self.assertEqual(sweep.sweep_progress(None), 0)
        self.assertEqual(sweep.sweep_progress(""), 0)

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(sweep.sweep_progress(os.path.join(self.dir, "nope")), 0)


class StagingSweepTest(Sandbox):
    def staging(self, name):
        path = os.path.join(self.dir, name)
        os.makedirs(path)
        self.touch(os.path.join(path, "partial.mkv"))
        return path

    def test_it_reclaims_an_abandoned_staging_directory(self):
        self.set_boot(int(time.time()) - 3600)
        self.staging("dl_{0}_abc123".format(DEAD_PID))
        mine = "dl_{0}_def456".format(os.getpid())
        self.staging(mine)
        self.assertEqual(sweep.sweep_staging(self.dir, self.log), 1)
        self.assertEqual(os.listdir(self.dir), [mine])
        self.assertIn(
            "[CLEANUP] Removed 1 abandoned staging dir(s) from " + self.dir,
            self.messages(),
        )

    def test_it_says_nothing_when_there_is_nothing_to_do(self):
        """An idle run costs one line; a sweep with no work must not add one."""
        self.set_boot(int(time.time()) - 3600)
        self.assertEqual(sweep.sweep_staging(self.dir, self.log), 0)
        self.assertEqual(self.logged, [])

    def test_a_directory_from_before_the_last_boot_goes(self):
        """The leak this guard exists for: a full-size media file in exactly
        the space check_disk_space measures."""
        now = int(time.time())
        self.set_boot(now - 60)
        path = self.staging("dl_{0}_ghi789".format(os.getpid()))
        os.utime(path, (now - 3600, now - 3600))
        self.assertEqual(sweep.sweep_staging(self.dir, self.log), 1)
        self.assertFalse(os.path.exists(path))

    def test_it_ignores_names_that_are_not_staging_directories(self):
        self.set_boot(int(time.time()) - 3600)
        self.touch(os.path.join(self.dir, "dl_notadir"))
        os.makedirs(os.path.join(self.dir, "unrelated"))
        self.assertEqual(sweep.sweep_staging(self.dir, self.log), 0)


class TombstoneSweepTest(Sandbox):
    def test_it_removes_what_macos_gave_up_on_and_reports_the_size(self):
        path = os.path.join(self.dir, ".smbdeleteAAA0x1234")
        with open(path, "wb") as handle:
            handle.write(b"\0" * 4096)
        self.assertEqual(sweep.sweep_tombstones(self.dir, self.log), 1)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(
            self.messages(),
            ["[SMBDELETE] Removed 1 delete-pending file(s) macOS left behind, 4.0KiB"],
        )

    def test_nothing_to_do_is_debug_only(self):
        self.assertEqual(sweep.sweep_tombstones(self.dir, self.log), 0)
        self.assertEqual(self.logged, [("DEBUG", "[SMBDELETE] No delete-pending files left behind")])

    def test_it_leaves_ordinary_files_alone(self):
        keep = os.path.join(self.dir, "Film.2026.mkv")
        self.touch(keep)
        self.assertEqual(sweep.sweep_tombstones(self.dir, self.log), 0)
        self.assertTrue(os.path.exists(keep))

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(sweep.sweep_tombstones(os.path.join(self.dir, "nope"), self.log), 0)

    def test_the_byte_rendering(self):
        self.assertEqual(sweep._human_bytes(0), "0 bytes")
        self.assertEqual(sweep._human_bytes(1023), "1023 bytes")
        self.assertEqual(sweep._human_bytes(1024), "1.0KiB")
        self.assertEqual(sweep._human_bytes(31 * 1024 ** 3), "31.0GiB")


class MarkerSweepTest(Sandbox):
    def marker(self, base):
        self.touch(os.path.join(self.dir, ".extracted_" + base))

    def test_a_marker_whose_archive_is_gone_is_removed(self):
        self.marker("Film.2026")
        self.assertEqual(sweep.sweep_markers(self.dir, self.log), 1)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIn("[MARKER] Cleaned up 1 of 1 extraction markers", self.messages())

    def test_a_marker_with_its_archive_still_there_stays(self):
        self.marker("Film.2026")
        self.touch(os.path.join(self.dir, "Film.2026.rar"))
        self.assertEqual(sweep.sweep_markers(self.dir, self.log), 0)
        self.assertIn("[MARKER] Cleaned up 0 of 1 extraction markers", self.messages())

    def test_a_multipart_set_counts_as_present(self):
        self.marker("Film.2026")
        self.touch(os.path.join(self.dir, "Film.2026.r07"))
        self.assertEqual(sweep.sweep_markers(self.dir, self.log), 0)

    def test_no_markers_says_nothing(self):
        """Bash's ``if [ $marker_count -gt 0 ]`` — what keeps an idle run to
        one line."""
        self.assertEqual(sweep.sweep_markers(self.dir, self.log), 0)
        self.assertEqual(self.logged, [])

    def test_an_unreadable_directory_says_so_rather_than_claiming_a_sweep(self):
        """A dropped SMB share reaches here.  Bash's glob would match nothing
        and it would sweep nothing, silently."""
        missing = os.path.join(self.dir, "nope")
        self.assertEqual(sweep.sweep_markers(missing, self.log), 0)
        self.assertEqual(self.logged[0][0], "DEBUG")
        self.assertIn("Could not read", self.logged[0][1])


if __name__ == "__main__":
    unittest.main()
