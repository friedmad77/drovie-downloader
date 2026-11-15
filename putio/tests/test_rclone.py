"""rclone's argv and its JSON — the flags a transfer actually runs with."""

from __future__ import annotations

import json
import unittest

from putio.config import Config
from putio.proc import CommandResult
from putio.rclone import RcloneClient, parse_lsjson


class FakeRunner:
    """Stands in for :func:`putio.proc.run`, recording every argv."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for key, value in self.answers.items():
            if key in argv:
                return value
        return CommandResult(argv, 0, "", "")


class FakeRetrier:
    def __init__(self, runner):
        self.runner = runner

    def run(self, description, argv, **kwargs):
        self.descriptions = getattr(self, "descriptions", [])
        self.descriptions.append(description)
        return self.runner(argv)


class WithFakeRclone(unittest.TestCase):
    """Swaps the module's runner for a fake, and puts the real one back.

    Restoring matters: this patches a module global, and a test file that left
    it patched would silently disarm every other test that shells out.
    """

    def setUp(self):
        import putio.rclone as module

        self._module = module
        self._real_run = module.run
        self.addCleanup(setattr, module, "run", self._real_run)

    def client(self, runner, **overrides):
        settings = dict(
            remote="putio:",
            rclone_cmd="/opt/homebrew/bin/rclone",
            rclone_transfers=4,
            rclone_checkers=8,
            rclone_multi_threads=2,
            rclone_buffer_size="64M",
            rclone_bwlimit="off",
            rclone_stats_interval="5s",
        )
        settings.update(overrides)
        self._module.run = runner
        return RcloneClient(Config(**settings), FakeRetrier(runner))


class ParseLsjsonTest(unittest.TestCase):
    def test_sizes_come_back_keyed_by_path(self):
        text = json.dumps(
            [
                {"Path": "a/Film.mkv", "Size": 100, "IsDir": False},
                {"Path": "a", "Size": -1, "IsDir": True},
            ]
        )
        self.assertEqual(parse_lsjson(text), {"a/Film.mkv": 100})

    def test_a_negative_size_is_no_answer_rather_than_a_number(self):
        """A negative size that passed through would sail past the disk check."""
        text = json.dumps([{"Path": "x.mkv", "Size": -1}])
        self.assertEqual(parse_lsjson(text), {})

    def test_garbage_is_an_empty_answer_not_an_exception(self):
        self.assertEqual(parse_lsjson("not json"), {})
        self.assertEqual(parse_lsjson('{"not": "a list"}'), {})


class SizeTest(WithFakeRclone):
    def test_one_recursive_listing_answers_for_every_file(self):
        """Bash runs lsjson twice per file — once in is_junk_file and once in
        download_file_run — each piped through jq."""
        payload = json.dumps(
            [{"Path": "a.mkv", "Size": 10}, {"Path": "b.mkv", "Size": 20}]
        )
        runner = FakeRunner({"lsjson": CommandResult([], 0, payload, "")})
        rclone = self.client(runner)
        self.assertEqual(rclone.size_of("a.mkv"), 10)
        self.assertEqual(rclone.size_of("b.mkv"), 20)
        self.assertEqual(len([c for c in runner.calls if "lsjson" in c]), 1)

    def test_a_miss_falls_back_to_the_single_lookup(self):
        listing = json.dumps([{"Path": "a.mkv", "Size": 10}])
        single = json.dumps([{"Path": "late.mkv", "Size": 99}])
        calls = {"n": 0}

        def runner(argv, **kwargs):
            calls["n"] += 1
            return CommandResult(argv, 0, listing if calls["n"] == 1 else single, "")

        rclone = self.client(runner)
        self.assertEqual(rclone.size_of("late.mkv"), 99)

    def test_a_failed_listing_is_no_answer_rather_than_zero(self):
        runner = FakeRunner({"lsjson": CommandResult([], 1, "", "boom")})
        self.assertIsNone(self.client(runner).size_of("a.mkv"))


class CopyArgvTest(WithFakeRclone):
    def _argv(self, **kwargs):
        runner = FakeRunner()
        rclone = self.client(runner)
        rclone.copy("Film.mkv", "/dest", kwargs.pop("log_path", ""), **kwargs)
        return runner.calls[-1]

    def test_the_flags_are_bashs(self):
        argv = self._argv(inplace=False)
        self.assertEqual(argv[:5], ["nice", "-n", "10", "/opt/homebrew/bin/rclone", "copy"])
        for flag in (
            "--transfers=4",
            "--checkers=8",
            "--multi-thread-streams=2",
            "--buffer-size=64M",
            "--bwlimit=off",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ["putio:Film.mkv", "/dest"])

    def test_inplace_is_the_direct_path_only(self):
        """Bash sets it downloading straight to the share and not when staging,
        where the file is already landing on local disk."""
        self.assertIn("--inplace", self._argv(inplace=True))
        self.assertNotIn("--inplace", self._argv(inplace=False))

    def test_no_log_file_flag_without_a_log(self):
        """rclone rejects --log-file="" outright, and without a log file there
        is nothing for --use-json-log to write to."""
        argv = self._argv(inplace=False, log_path="")
        self.assertFalse([a for a in argv if a.startswith("--log-file")])
        self.assertNotIn("--use-json-log", argv)

    def test_the_progress_flags_arrive_together(self):
        argv = self._argv(inplace=False, log_path="/tmp/p/dl.1.1.log")
        self.assertIn("--log-file=/tmp/p/dl.1.1.log", argv)
        self.assertIn("--use-json-log", argv)
        self.assertIn("--stats=5s", argv)
        self.assertIn("--stats-log-level", argv)

    def test_the_binary_is_the_pin_never_the_bare_word(self):
        runner = FakeRunner()
        rclone = self.client(runner)
        rclone.delete("Film.mkv", "delete from put.io")
        self.assertEqual(runner.calls[-1][0], "/opt/homebrew/bin/rclone")


if __name__ == "__main__":
    unittest.main()
