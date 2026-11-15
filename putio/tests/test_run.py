"""The run — validation, the summary line, cleanup, and the empty-folder pass.

The download loop itself is covered by ``test_pipeline.py`` and by
``tests/progress_harness.sh``, which runs real rclone transfers.  What is here
is everything phase 5 moved out of bash's main flow, plus the two things the
outside world reads: the completion phrase and the exit code.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest

from putio import run as run_module
from putio.config import Config
from putio.run import COMPLETION_PHRASE, Run


class FakeRclone:
    def __init__(self, dirs=None, contents=None):
        self._dirs = dirs if dirs is not None else []
        self._contents = contents or {}
        self.removed = []
        self.rmdir_fails = set()

    def list_dirs(self):
        return list(self._dirs)

    def list_dir(self, subdir):
        return self._contents.get(subdir)

    def rmdir(self, path, _description):
        if path in self.rmdir_fails:
            return False
        self.removed.append(path)
        return True


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-run-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logged = []

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def log(self, level, message):
        self.logged.append((level, message))

    def messages(self):
        return [message for _level, message in self.logged]

    def config(self, **overrides):
        settings = dict(
            local_dir=self.dir,
            unmatched_dir=self.path("unmatched"),
            log_file=self.path("run.log"),
            lockfile=self.path("run.lock"),
            tmdb_api_key="key",
            plex_token="token",
            rclone_cmd="/usr/bin/true",
            rclone_stats_interval="5s",
            media_mount_point="",
        )
        settings.update(overrides)
        return Config(**settings)

    def run_for(self, **overrides):
        run = Run(self.config(**overrides), self.log)
        return run


# ---------------------------------------------------------------------------
# the summary line
# ---------------------------------------------------------------------------
class SummaryTest(Sandbox):
    def summary(self, **state):
        run = self.run_for()
        for key, value in state.items():
            setattr(run, key, value)
        return run.summarise()

    def test_the_phrase_is_verbatim(self):
        """``putioctl status`` greps the log for this.  It is an invariant no
        phase may move, and this test is why."""
        self.assertEqual(COMPLETION_PHRASE, "Script completed successfully")
        self.assertIn("Script completed successfully", self.summary())

    def test_an_idle_run_says_nothing_to_do(self):
        self.assertIn("— nothing to do (", self.summary())

    def test_it_counts_what_landed(self):
        self.assertIn("— 3 downloaded (", self.summary(downloaded=3))

    def test_files_offered_but_none_downloaded_is_called_out(self):
        """The combination worth noticing: a run whose share dropped
        mid-dispatch, or whose transfers all failed their retries, must not
        fall through to "nothing to do"."""
        self.assertIn(
            "— nothing downloaded (4 file(s) on put.io) (",
            self.summary(downloaded=0, putio_file_count=4),
        )

    def test_local_leftovers_and_the_plex_scan_join_on(self):
        line = self.summary(downloaded=3, local_existing=1, plex_scanned="movies and TV")
        self.assertIn("— 3 downloaded, 1 local file(s), Plex scan: movies and TV (", line)

    def test_it_marks_the_run_completed(self):
        """What stops the cleanup logging "Exited without completing"."""
        run = self.run_for()
        self.assertFalse(run.completed)
        run.summarise()
        self.assertTrue(run.completed)

    def test_the_line_carries_an_elapsed_time(self):
        self.assertRegex(self.summary(), r"\(\d+[smh]")


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------
class ValidateTest(Sandbox):
    def test_a_healthy_config_has_no_errors(self):
        self.assertEqual(self.run_for().validate(), [])

    def test_a_missing_tmdb_key_is_an_error(self):
        self.assertIn(
            "ERROR: TMDB_API_KEY not set in configuration",
            self.run_for(tmdb_api_key="").validate(),
        )

    def test_a_missing_plex_token_is_an_error(self):
        self.assertIn(
            "ERROR: PLEX_TOKEN not set in configuration",
            self.run_for(plex_token="").validate(),
        )

    def test_a_missing_local_dir_is_an_error(self):
        errors = self.run_for(local_dir=self.path("nope")).validate()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("ERROR: LOCAL_DIR does not exist:"))

    def test_a_bad_stats_interval_warns_and_falls_back(self):
        """rclone exits at flag-parse time on a malformed duration, before
        --log-file opens — so every download would fail with no cause in the
        log.  This knob only drives the bar, so it falls back instead."""
        run = self.run_for(rclone_stats_interval="soon")
        self.assertEqual(run.validate(), [])
        self.assertEqual(run.config.rclone_stats_interval, "5s")
        self.assertIn(
            "[CONFIG] RCLONE_STATS_INTERVAL='soon' is not a duration — using 5s",
            self.messages(),
        )

    def test_the_corrected_interval_reaches_the_batch(self):
        """The half of the fallback that matters, and the half that was broken.

        ``validate()`` cannot repair the interval in place — Config is frozen —
        so it rebinds ``self.config``. ``execute()`` used to read its local
        copy *before* that, and handed the stale object to ``build_batch``: the
        warning was logged and rclone was still invoked with ``--stats=5sec``,
        which it rejects at flag-parse time, before ``--log-file`` opens, so
        every transfer in the run failed with no cause in the log.

        Asserting on the log line alone cannot see this. Asserting on what the
        rclone client was actually built with can.
        """
        run = self.run_for(
            rclone_stats_interval="5sec", rclone_cmd="/usr/bin/false", retry_delay=0
        )
        built = {}
        original = run_module.build_batch

        def spy(config, log, record, on_download):
            built["interval"] = config.rclone_stats_interval
            return original(config, log, record, on_download)

        run_module.build_batch = spy
        self.addCleanup(setattr, run_module, "build_batch", original)
        with contextlib.redirect_stdout(io.StringIO()):
            run.execute()
        self.assertEqual(built.get("interval"), "5s")

    def test_valid_durations_are_left_alone(self):
        for value in ("5s", "500ms", "2m", "1h", "10"):
            run = self.run_for(rclone_stats_interval=value)
            run.validate()
            self.assertEqual(run.config.rclone_stats_interval, value)

    def test_staging_is_only_checked_when_it_is_used(self):
        """USE_TEMP_DIR=false means nothing stages, so an unwritable
        STAGING_DIR is not this run's problem."""
        errors = self.run_for(
            use_temp_dir=False, staging_dir="/proc/nope/staging"
        ).validate()
        self.assertEqual(errors, [])

    def test_an_unwritable_staging_dir_is_an_error_when_it_is_used(self):
        """A USB volume cannot be remounted programmatically, so there is no
        point limping into the run."""
        errors = self.run_for(
            use_temp_dir=True, staging_dir="/dev/null/staging"
        ).validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("STAGING_DIR is not writable", errors[0])

    def test_errors_reach_the_log_as_well_as_stdout(self):
        """Bash echoed these and nothing else, so a run that refused to start
        over a missing key left nothing in the log saying why.  Additive:
        nothing greps these lines."""
        run = self.run_for(tmdb_api_key="", plex_token="")
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(run.execute(), 1)
        # cron mails stdout; that half is how the operator finds out at all.
        self.assertIn("TMDB_API_KEY not set", captured.getvalue())
        errors = [m for level, m in self.logged if level == "ERROR"]
        self.assertIn("ERROR: TMDB_API_KEY not set in configuration", errors)
        self.assertIn("ERROR: PLEX_TOKEN not set in configuration", errors)


class MountPreflightTest(Sandbox):
    """``validate_config``'s share check, which only runs when one is configured.

    This is the branch a sandbox with ``MEDIA_MOUNT_POINT=""`` never reaches —
    and it shipped broken once for exactly that reason, calling a method
    :class:`~putio.mount.MountGate` does not have. The first real run after
    deploying found it; these are so the next one does not have to.
    """

    def _gate(self, status):
        from putio import mount as mount_module

        original = mount_module.MountGate.probe
        mount_module.MountGate.probe = lambda self: status
        self.addCleanup(setattr, mount_module.MountGate, "probe", original)

    def _mount_table(self, present):
        original = run_module._in_mount_table
        run_module._in_mount_table = lambda point: present
        self.addCleanup(setattr, run_module, "_in_mount_table", original)

    def test_no_mount_point_means_no_check_at_all(self):
        self.assertEqual(self.run_for(media_mount_point="").validate(), [])

    def test_a_live_share_passes(self):
        self._gate(0)
        self.assertEqual(self.run_for(media_mount_point="/Volumes/X").validate(), [])

    def test_a_share_still_in_the_mount_table_only_warns(self):
        """Downstream I/O surfaces anything real, and the per-file gate still
        runs — so a failing health check on a share the kernel still has is not
        grounds to refuse the whole run."""
        self._gate(1)
        self._mount_table(True)
        run = self.run_for(media_mount_point="/Volumes/X")
        self.assertEqual(run.validate(), [])
        self.assertIn(
            "[MOUNT] Preflight stat failed but /Volumes/X is in mount table — proceeding",
            self.messages(),
        )

    def test_a_share_genuinely_gone_refuses_the_run(self):
        self._gate(1)
        self._mount_table(False)
        errors = self.run_for(media_mount_point="/Volumes/X").validate()
        self.assertEqual(
            errors,
            ["ERROR: media mount /Volumes/X is not available and could not be remounted"],
        )

    def test_the_preflight_does_not_condemn_the_share_for_the_rest_of_the_run(self):
        """It must not go through ``check``: that marks the gate dead, and the
        first file would then be skipped over a preflight the run tolerated."""
        from putio import mount as mount_module

        gate = mount_module.MountGate(
            self.config(media_mount_point="/Volumes/X"), "", self.log, runner=lambda: 1
        )
        self.assertEqual(gate.probe(), 1)
        self.assertFalse(gate.dead)


# ---------------------------------------------------------------------------
# empty folders
# ---------------------------------------------------------------------------
class EmptyFolderTest(Sandbox):
    def test_an_empty_folder_is_removed(self):
        rclone = FakeRclone(dirs=["Stale/"], contents={"Stale/": []})
        self.run_for().remove_empty_folders(rclone)
        self.assertEqual(rclone.removed, ["Stale/"])
        self.assertIn("[PUTIO] Deleted empty folder: Stale/", self.messages())

    def test_a_folder_with_files_in_it_is_left(self):
        rclone = FakeRclone(dirs=["Busy/"], contents={"Busy/": ["Film.mkv"]})
        self.run_for().remove_empty_folders(rclone)
        self.assertEqual(rclone.removed, [])

    def test_a_failed_listing_is_not_treated_as_empty(self):
        """None is "rclone failed", which is not the same as empty — deleting
        on a failed listing is how a folder with files in it disappears."""
        rclone = FakeRclone(dirs=["Unknown/"], contents={})
        self.run_for().remove_empty_folders(rclone)
        self.assertEqual(rclone.removed, [])

    def test_a_failed_rmdir_is_reported(self):
        rclone = FakeRclone(dirs=["Stale/"], contents={"Stale/": []})
        rclone.rmdir_fails.add("Stale/")
        self.run_for().remove_empty_folders(rclone)
        self.assertIn("[PUTIO] Failed to delete empty folder: Stale/", self.messages())

    def test_no_directories_at_all_is_quiet(self):
        rclone = FakeRclone(dirs=[])
        self.run_for().remove_empty_folders(rclone)
        self.assertEqual([m for level, m in self.logged if level != "DEBUG"], [])


# ---------------------------------------------------------------------------
# the record channel, now that both ends are in one process
# ---------------------------------------------------------------------------
class RecordTest(Sandbox):
    def test_added_drives_which_libraries_are_scanned(self):
        run = self.run_for()
        run._record("ADDED", "movie")
        run._record("ADDED", "movie")
        run._record("ADDED", "tv")
        self.assertEqual(run.added, {"movie", "tv"})

    def test_downloads_are_counted_not_named(self):
        run = self.run_for()
        run._note_download("A.mkv")
        run._note_download("B.mkv")
        self.assertEqual(run.downloaded, 2)

    def test_other_records_are_ignored(self):
        run = self.run_for()
        run._record("START")
        run._record("LOCAL", "3")
        run._record("DONE")
        self.assertEqual(run.added, set())

    def test_the_counters_survive_concurrent_workers(self):
        """``ADDED`` and the tally are both written from pool threads."""
        import threading

        run = self.run_for()

        def worker():
            for _ in range(200):
                run._note_download("x")
                run._record("ADDED", "movie")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(run.downloaded, 8 * 200)
        self.assertEqual(run.added, {"movie"})


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------
class CleanupTest(Sandbox):
    def test_an_incomplete_run_leaves_a_trace(self):
        """The start banner is DEBUG, so without this a run that died partway
        would look indistinguishable from cron never firing."""
        run = self.run_for()
        run.cleanup(1)
        self.assertIn("[RUN] Exited without completing (status 1)", self.messages())

    def test_a_completed_run_says_nothing_extra(self):
        run = self.run_for()
        run.completed = True
        run.cleanup(0)
        self.assertEqual(self.logged, [])

    def test_it_releases_the_lock(self):
        run = self.run_for()
        run._lock.acquire()
        self.assertTrue(os.path.exists(run.config.lockfile))
        run.cleanup(0)
        self.assertFalse(os.path.exists(run.config.lockfile))

    def test_it_clears_the_bars_this_run_published(self):
        state_dir = self.path("progress")
        os.makedirs(state_dir)
        run = self.run_for(progress_state_dir=state_dir, run_pid="4242")
        from putio.progress import ProgressPublisher

        run._progress = ProgressPublisher(state_dir, "4242")
        open(os.path.join(state_dir, "dl.4242.1.meta"), "w").close()
        open(os.path.join(state_dir, "dl.9999.1.meta"), "w").close()
        run.cleanup(0)
        self.assertEqual(os.listdir(state_dir), ["dl.9999.1.meta"])


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
class MainTest(Sandbox):
    def env(self, **overrides):
        settings = {
            "LOCAL_DIR": self.dir,
            "UNMATCHED_DIR": self.path("unmatched"),
            "LOG_FILE": self.path("run.log"),
            "LOCKFILE": self.path("run.lock"),
            "PAUSE_FILE": self.path("paused"),
            "PROGRESS_STATE_DIR": "",
            "TMDB_API_KEY": "key",
            "PLEX_TOKEN": "token",
            "RCLONE_CMD": "/usr/bin/false",
            # Every rclone call is retried; a real 5s -> 10s ladder would
            # spend half a minute of the suite asleep.
            "RETRY_DELAY": "0",
        }
        settings.update(overrides)
        return settings

    def _with_env(self, env):
        original = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))

    def main(self):
        """``run_module.main`` with its own logger, whose stdout half would
        otherwise land in the middle of the suite's output."""
        with contextlib.redirect_stdout(io.StringIO()):
            return run_module.main()

    def test_a_paused_run_exits_zero_and_takes_no_lock(self):
        """cron mails failures, and a run skipped deliberately is not one.  It
        is checked before the lock so a paused tick leaves no trace at all."""
        with open(self.path("paused"), "w") as handle:
            handle.write("PAUSED_UNTIL=0\n")
        self._with_env(self.env())
        self.assertEqual(self.main(), 0)
        self.assertFalse(os.path.exists(self.path("run.lock")))

    def test_a_held_lock_exits_zero_and_is_not_an_error(self):
        """cron fires every few minutes and a queue of transfers runs for
        hours, so a tick that finds the lock held is the schedule working, not
        a failure.  It says who holds it and for how long, which is the only
        thing that separates an ordinary overlap from a wedged run."""
        with open(self.path("run.lock"), "w") as handle:
            handle.write("{0}\n".format(os.getpid()))
        self._with_env(self.env())
        self.assertEqual(self.main(), 0)
        with open(self.path("run.log")) as handle:
            body = handle.read()
        self.assertIn("[LOCK] A sync is already running (PID {0}".format(os.getpid()), body)
        self.assertIn("[INFO]", body)
        self.assertNotIn("[ERROR]", body)

    def test_a_held_lock_line_carries_the_holder_s_elapsed_time(self):
        with open(self.path("run.lock"), "w") as handle:
            handle.write("{0}\n".format(os.getpid()))
        self._with_env(self.env())
        self.main()
        with open(self.path("run.log")) as handle:
            body = handle.read()
        self.assertRegex(body, r"elapsed [0-9:\-]+\) — skipping this run")

    def test_a_failed_listing_exits_non_zero_and_releases_the_lock(self):
        self._with_env(self.env())
        self.assertEqual(self.main(), 1)
        self.assertFalse(os.path.exists(self.path("run.lock")))
        with open(self.path("run.log")) as handle:
            body = handle.read()
        self.assertIn("[PUTIO] Failed to list files from put.io", body)

    def test_a_failed_listing_is_retried_and_says_why(self):
        """It once said only "Failed to list files", because bash discarded
        rclone's stderr — a one-off put.io blip left nothing to diagnose."""
        fake = self.path("rclone")
        with open(fake, "w") as handle:
            handle.write("#!/bin/sh\necho 'Failed to lsf: 502 Bad Gateway' >&2\nexit 1\n")
        os.chmod(fake, 0o755)
        self._with_env(self.env(RCLONE_CMD=fake, MAX_RETRIES="3"))
        self.assertEqual(self.main(), 1)
        with open(self.path("run.log")) as handle:
            body = handle.read()
        self.assertIn("502 Bad Gateway", body)
        self.assertIn("[RETRY] All 3 attempts failed: list files on put.io", body)

    def test_an_unhandled_error_still_releases_the_lock(self):
        """The lock outliving a crash would make every later cron tick exit as
        "already running" until someone noticed."""
        self._with_env(self.env())
        original = Run.execute

        def boom(self):
            raise RuntimeError("kaboom")

        Run.execute = boom
        self.addCleanup(setattr, Run, "execute", original)
        self.assertEqual(self.main(), 1)
        self.assertFalse(os.path.exists(self.path("run.lock")))
        with open(self.path("run.log")) as handle:
            self.assertIn("kaboom", handle.read())


if __name__ == "__main__":
    unittest.main()
