"""The retry ladder, and the five lines a run's log is read against."""

from __future__ import annotations

import sys
import unittest

from putio.proc import CommandResult, Retrier, run


class FakeCommand:
    """A command that fails a given number of times, then succeeds."""

    def __init__(self, failures, stderr=""):
        self.failures = failures
        self.stderr = stderr
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            return CommandResult(argv, 1, "", self.stderr)
        return CommandResult(argv, 0, "ok", "")


class RetrierTest(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.lines = []

    def _retrier(self, command, max_attempts=3, delay=5):
        retrier = Retrier(
            max_attempts=max_attempts,
            delay=delay,
            on_log=lambda level, message: self.lines.append((level, message)),
            sleep=self.slept.append,
        )
        # Swap the module-level runner for the fake, the way the download path
        # would be swapped for a fake rclone.
        import putio.proc as proc

        self._saved = proc.run
        proc.run = command
        self.addCleanup(setattr, proc, "run", self._saved)
        return retrier

    def test_first_attempt_success_says_nothing(self):
        command = FakeCommand(failures=0)
        result = self._retrier(command).run("download X", ["true"])
        self.assertTrue(result.ok)
        self.assertEqual(self.lines, [])
        self.assertEqual(self.slept, [])

    def test_the_ladder_is_five_then_ten_then_twenty(self):
        command = FakeCommand(failures=2)
        result = self._retrier(command).run("download X", ["true"])
        self.assertTrue(result.ok)
        self.assertEqual(self.slept, [5, 10])

    def test_every_bash_message_is_reproduced(self):
        command = FakeCommand(failures=1, stderr="boom\n")
        self._retrier(command).run("download X", ["true"])
        messages = [message for _level, message in self.lines]
        self.assertIn("[RETRY] Error output: boom\n", messages)
        self.assertIn(
            "[RETRY] Attempt 1 failed: download X. Retrying in 5s...", messages
        )
        self.assertIn("[RETRY] Attempt 2/3: download X", messages)
        self.assertIn("[RETRY] Success on attempt 2: download X", messages)

    def test_exhaustion_says_so_once(self):
        command = FakeCommand(failures=99)
        result = self._retrier(command).run("delete Y", ["false"])
        self.assertFalse(result.ok)
        messages = [message for _level, message in self.lines]
        self.assertIn("[RETRY] All 3 attempts failed: delete Y", messages)
        self.assertEqual(command.calls, 3)

    def test_empty_stderr_is_not_logged(self):
        """rclone's stderr is empty on the download path — --log-file takes
        everything — so an "Error output:" line with nothing after it would be
        the norm rather than the exception."""
        command = FakeCommand(failures=1, stderr="   \n")
        self._retrier(command).run("download X", ["true"])
        messages = [message for _level, message in self.lines]
        self.assertFalse(any(m.startswith("[RETRY] Error output:") for m in messages))

    def test_a_max_of_zero_still_makes_one_attempt(self):
        """A config that omits MAX_RETRIES makes bash's `while` a syntax error
        and every retried command fail with nothing naming the cause.  Here the
        floor is one honest attempt."""
        command = FakeCommand(failures=0)
        result = self._retrier(command, max_attempts=0).run("x", ["true"])
        self.assertTrue(result.ok)
        self.assertEqual(command.calls, 1)


class RunTest(unittest.TestCase):
    def test_a_missing_binary_is_127(self):
        result = run(["/nonexistent/definitely-not-here"])
        self.assertEqual(result.returncode, 127)
        self.assertFalse(result.ok)

    def test_output_comes_back_decoded(self):
        result = run([sys.executable, "-c", "print('hello')"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_argv_is_never_re_split(self):
        """Bash runs ``$RCLONE_CMD`` unquoted, so a pin under a path with a
        space becomes two words and the run dies naming only the first."""
        result = run([sys.executable, "-c", "import sys; print(len(sys.argv))", "a b"])
        # argv is ["-c", "a b"]: one argument, not the two a shell would split
        # "a b" into.
        self.assertEqual(result.stdout.strip(), "2")

    def test_a_timeout_reports_124(self):
        result = run(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2
        )
        self.assertEqual(result.returncode, 124)


if __name__ == "__main__":
    unittest.main()
