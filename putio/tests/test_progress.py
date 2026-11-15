"""The progress protocol — the format three readers depend on.

``putioctl progress``, the curses console and ``putio-statusd`` all parse these
files and none of them changed in this phase, so "byte-identical" is the whole
test.  The bash side is pinned separately by ``tests/progress_harness.sh``,
which reads a file this code wrote with the same ``read``-based parser
``putioctl`` uses.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from putio.progress import Job, NullJob, ProgressPublisher, error_tail


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-progress-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class MetaFormatTest(Sandbox):
    def _fields(self, path):
        fields = {}
        with open(path) as handle:
            for line in handle:
                key, _, value = line.rstrip("\n").partition("=")
                fields[key] = value
        return fields

    def test_the_file_carries_progress_writes_eight_keys_in_order(self):
        publisher = ProgressPublisher(self.dir, "4242")
        job = publisher.begin("Some/Film.2026.mkv", "8461397504", "/local/Film.2026.mkv")
        with open(job.meta_path) as handle:
            keys = [line.split("=", 1)[0] for line in handle.read().splitlines()]
        self.assertEqual(
            keys,
            ["RUN_PID", "PHASE", "STARTED", "SIZE", "NAME", "REMOTE", "DEST", "LOG"],
        )

    def test_it_is_published_under_the_runs_pid_not_this_process(self):
        """Readers call ``kill -0`` on field 2 of the *name* and on RUN_PID.
        Publishing under the interpreter's pid would make every bar vanish the
        instant the batch ended."""
        publisher = ProgressPublisher(self.dir, "4242")
        job = publisher.begin("Film.mkv", "10", "/local/Film.mkv")
        self.assertEqual(os.path.basename(job.meta_path).split(".")[1], "4242")
        self.assertEqual(self._fields(job.meta_path)["RUN_PID"], "4242")
        self.assertNotEqual("4242", str(os.getpid()))

    def test_name_is_the_basename_and_remote_is_whole(self):
        publisher = ProgressPublisher(self.dir, "1")
        job = publisher.begin("dir/sub/Film.2026.mkv", "10", "/local/Film.2026.mkv")
        fields = self._fields(job.meta_path)
        self.assertEqual(fields["NAME"], "Film.2026.mkv")
        self.assertEqual(fields["REMOTE"], "dir/sub/Film.2026.mkv")

    def test_the_log_path_sits_beside_the_meta_file(self):
        publisher = ProgressPublisher(self.dir, "7")
        job = publisher.begin("Film.mkv", "10", "/local/Film.mkv")
        self.assertEqual(job.meta_path[: -len(".meta")], job.log_path[: -len(".log")])
        self.assertEqual(self._fields(job.meta_path)["LOG"], job.log_path)
        self.assertTrue(os.path.exists(job.log_path))

    def test_a_name_with_spaces_and_quotes_survives(self):
        publisher = ProgressPublisher(self.dir, "1")
        weird = 'Some "Film" (2026) — director\'s cut.mkv'
        job = publisher.begin(weird, "10", "/local/x.mkv")
        self.assertEqual(self._fields(job.meta_path)["NAME"], weird)

    def test_the_phase_change_rewrites_dest(self):
        publisher = ProgressPublisher(self.dir, "1")
        job = publisher.begin("Film.mkv", "10", "/staging/Film.mkv")
        job.phase("copy", "/share/Film.mkv")
        fields = self._fields(job.meta_path)
        self.assertEqual(fields["PHASE"], "copy")
        self.assertEqual(fields["DEST"], "/share/Film.mkv")

    def test_no_tmp_file_is_left_behind(self):
        publisher = ProgressPublisher(self.dir, "1")
        publisher.begin("Film.mkv", "10", "/local/Film.mkv")
        self.assertFalse([n for n in os.listdir(self.dir) if n.endswith(".tmp")])


class JobIdTest(Sandbox):
    def test_concurrent_jobs_never_share_a_name(self):
        """Bash asks ``sh -c 'echo $PPID'`` for a job id, which names a process
        that has already exited and whose number the kernel can reissue — two
        jobs really can collide.  A counter cannot."""
        publisher = ProgressPublisher(self.dir, "9")
        names = {publisher.begin("f{0}".format(i), "1", "/x").meta_path for i in range(50)}
        self.assertEqual(len(names), 50)


class ClearTest(Sandbox):
    def test_a_finished_job_removes_both_of_its_files(self):
        publisher = ProgressPublisher(self.dir, "5")
        with publisher.begin("Film.mkv", "10", "/local/Film.mkv") as job:
            self.assertTrue(os.path.exists(job.meta_path))
        self.assertFalse(os.path.exists(job.meta_path))
        self.assertFalse(os.path.exists(job.log_path))

    def test_the_context_manager_clears_on_an_exception_too(self):
        """The whole reason bash wraps download_file_run: the body returns from
        a dozen places and a missed one leaves a finished transfer at 90% in
        every reader until the run exits."""
        publisher = ProgressPublisher(self.dir, "5")
        job = publisher.begin("Film.mkv", "10", "/local/Film.mkv")
        try:
            with job:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(os.path.exists(job.meta_path))

    def test_clear_all_takes_only_this_runs_files(self):
        mine = ProgressPublisher(self.dir, "100")
        theirs = ProgressPublisher(self.dir, "200")
        mine.begin("a.mkv", "1", "/x")
        other = theirs.begin("b.mkv", "1", "/x")
        mine.clear_all()
        self.assertTrue(os.path.exists(other.meta_path))


class DisabledTest(Sandbox):
    def test_an_empty_state_dir_publishes_nothing(self):
        """Empty is a real setting, not "unset" — see putio.config."""
        publisher = ProgressPublisher("", "1")
        job = publisher.begin("Film.mkv", "10", "/local/Film.mkv")
        self.assertIsInstance(job, NullJob)
        self.assertEqual(job.log_path, "")
        self.assertEqual(job.error_tail(), "")


class ErrorTailTest(Sandbox):
    def _log(self, lines):
        path = os.path.join(self.dir, "dl.1.1.log")
        with open(path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def test_stats_lines_are_ignored(self):
        path = self._log(
            [
                json.dumps({"stats": {"bytes": 1}, "msg": "Transferred: 1"}),
                json.dumps({"msg": "directory not found"}),
            ]
        )
        self.assertEqual(error_tail(path), " — directory not found")

    def test_repeated_messages_collapse_and_only_two_survive(self):
        """rclone repeats itself once per attempt, and this rides along on one
        log line rather than being the transcript."""
        path = self._log(
            [
                json.dumps({"msg": "first"}),
                json.dumps({"msg": "same"}),
                json.dumps({"msg": "same"}),
                json.dumps({"msg": "last"}),
            ]
        )
        self.assertEqual(error_tail(path), " — same last")

    def test_it_is_capped(self):
        path = self._log([json.dumps({"msg": "x" * 900})])
        self.assertEqual(len(error_tail(path)), 300 + len(" — "))

    def test_unparseable_lines_are_skipped_rather_than_fatal(self):
        path = self._log(["not json at all", json.dumps({"msg": "real"})])
        self.assertEqual(error_tail(path), " — real")

    def test_an_empty_or_missing_log_contributes_nothing(self):
        self.assertEqual(error_tail(""), "")
        self.assertEqual(error_tail(os.path.join(self.dir, "nope.log")), "")
        self.assertEqual(error_tail(self._log([])), "")

    def test_only_the_last_eight_non_stats_lines_are_considered(self):
        lines = [json.dumps({"msg": "old"})] + [
            json.dumps({"msg": "m{0}".format(i)}) for i in range(8)
        ]
        self.assertEqual(error_tail(self._log(lines)), " — m6 m7")


if __name__ == "__main__":
    unittest.main()
