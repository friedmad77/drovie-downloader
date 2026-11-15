"""The log format and the rotation — both of them invariants, not choices.

``rotate_log`` compares the timestamps it writes *as strings*, ``putioctl
status`` greps the file for a phrase, the console filters on the level, and
the shim still writes into the same file from ``--mount-gate`` in bash.  So
the assertions here are deliberately about bytes.

The rotation algorithm is also a performance contract: parsing each line back
into an epoch once cost 36 seconds of every idle run.  There is a test below
that a log with no work in it is not rewritten at all.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from putio import logs
from putio.logs import Logger, format_duration, rotate


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-logs-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "processor.log")

    def write(self, text):
        with open(self.path, "wb") as handle:
            handle.write(text if isinstance(text, bytes) else text.encode())

    def read(self):
        with open(self.path, "rb") as handle:
            return handle.read()


class Stream:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# the line
# ---------------------------------------------------------------------------
class LineFormatTest(Sandbox):
    def logger(self, **kwargs):
        self.stream = Stream()
        options = dict(
            log_file=self.path,
            level="DEBUG",
            stream=self.stream,
            rotate_on_write=False,
        )
        options.update(kwargs)
        return Logger(**options)

    def test_the_format_is_the_one_rotate_log_compares(self):
        """``[YYYY-MM-DD HH:MM:SS] [LEVEL] message``, zero-padded so lexical
        order is chronological order."""
        self.logger()("INFO", "[PUTIO] Found 3 files")
        line = self.read().decode().rstrip("\n")
        self.assertRegex(
            line,
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[INFO\] \[PUTIO\] Found 3 files$",
        )

    def test_it_tees_to_the_stream_and_the_file(self):
        """``log`` piped through ``tee -a``: cron mails stdout, and the file is
        what ``putioctl logs`` reads."""
        logger = self.logger()
        logger("WARN", "something")
        self.assertEqual(len(self.stream.lines), 1)
        self.assertIn(b"something", self.read())
        self.assertEqual(self.stream.lines[0].rstrip("\n").encode(), self.read().rstrip(b"\n"))

    def test_a_level_below_the_threshold_writes_nothing(self):
        logger = self.logger(level="INFO")
        logger("DEBUG", "chatter")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.stream.lines, [])

    def test_an_unknown_threshold_reads_as_info(self):
        """``get_log_level_num``'s ``*) echo 3``, for the threshold as well as
        for the message level."""
        logger = self.logger(level="LOUD")
        logger("INFO", "kept")
        logger("DEBUG", "dropped")
        self.assertIn(b"kept", self.read())
        self.assertNotIn(b"dropped", self.read())

    def test_a_name_that_is_not_utf8_survives(self):
        """put.io produces these, and rclone's stderr lands in this file
        verbatim.  Encoding must not be able to fail."""
        name = os.fsdecode(b"Film \xff\xfe.mkv")
        self.logger()("INFO", "[DOWNLOAD] " + name)
        self.assertIn(b"Film \xff\xfe.mkv", self.read())

    def test_an_unwritable_log_file_does_not_stop_the_run(self):
        """LOG_FILE on a share that just dropped.  The transfers report their
        own failures; losing the log must not lose the run."""
        logger = self.logger(log_file=os.path.join(self.dir, "no-such-dir", "x.log"))
        logger("ERROR", "still logged to stdout")
        self.assertEqual(len(self.stream.lines), 1)

    def test_no_log_file_at_all_is_stdout_only(self):
        logger = self.logger(log_file="")
        logger("INFO", "hello")
        self.assertEqual(len(self.stream.lines), 1)

    def test_rotation_fires_on_the_hundredth_call_and_not_otherwise(self):
        rotated = []
        original = logs.rotate
        logs.rotate = lambda path, *a, **k: rotated.append(path)
        self.addCleanup(setattr, logs, "rotate", original)

        draws = iter([7, 0, 42])
        logger = Logger(
            self.path,
            "DEBUG",
            stream=Stream(),
            rotate_on_write=True,
            _random=lambda n: next(draws),
        )
        for _ in range(3):
            logger("INFO", "x")
        self.assertEqual(rotated, [self.path])


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------
def stamp(offset_seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_seconds))


class RotateTest(Sandbox):
    def test_lines_older_than_a_day_go_and_newer_ones_stay(self):
        self.write(
            "[{0}] [INFO] ancient\n[{1}] [INFO] recent\n".format(
                stamp(-90000), stamp(-60)
            )
        )
        self.assertTrue(rotate(self.path))
        kept = self.read().decode()
        self.assertNotIn("ancient", kept)
        self.assertIn("recent", kept)

    def test_an_untimestamped_line_inherits_the_decision_above_it(self):
        """Raw rclone stderr and multi-line continuations have no stamp of
        their own, so ancient rclone output must not ride along forever."""
        self.write(
            "[{0}] [ERROR] old\n  continuation of old\n"
            "[{1}] [ERROR] new\n  continuation of new\n".format(stamp(-90000), stamp(-60))
        )
        rotate(self.path)
        kept = self.read().decode()
        self.assertNotIn("continuation of old", kept)
        self.assertIn("continuation of new", kept)

    def test_untimestamped_lines_before_any_stamp_are_dropped(self):
        """``keeping`` starts false, so a fragment at the head of the file has
        nothing to inherit a decision from and goes."""
        recent = stamp(-60)
        self.write("orphan with no stamp\n[{0}] [INFO] new\n".format(recent))
        rotate(self.path)
        self.assertEqual(self.read().decode(), "[{0}] [INFO] new\n".format(recent))

    def test_everything_expiring_truncates_rather_than_deleting(self):
        """``> "$LOG_FILE"`` — the file has to stay, because the next `log`
        call appends to it and `putioctl` stats it."""
        self.write("[{0}] [INFO] ancient\n".format(stamp(-90000)))
        rotate(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.read(), b"")

    def test_a_log_that_is_not_utf8_rotates_without_decoding(self):
        self.write(
            b"[" + stamp(-60).encode() + b"] [ERROR] \xff\xfe raw rclone bytes\n"
        )
        rotate(self.path)
        self.assertIn(b"\xff\xfe", self.read())

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(rotate(os.path.join(self.dir, "nope.log")))

    def test_no_temp_file_is_left_behind(self):
        self.write("[{0}] [INFO] recent\n".format(stamp(-60)))
        rotate(self.path)
        self.assertEqual(os.listdir(self.dir), ["processor.log"])

    def test_the_replacement_is_atomic_within_the_directory(self):
        """Beside the log, not in /tmp: a cross-device ``mv`` is a copy, so a
        reader could see a half-written log and a full /tmp could lose it."""
        self.write("[{0}] [INFO] recent\n".format(stamp(-60)))
        before = os.stat(self.dir).st_dev
        rotate(self.path)
        self.assertEqual(os.stat(self.path).st_dev, before)

    def test_rotation_does_no_per_line_subprocess_work(self):
        """The performance contract, asserted the only way a unit test can:
        a large log rotates in well under the time a fork per line would take.

        36 seconds of every idle run went on a ``sed`` and two ``date`` calls
        per line before the string compare replaced them.  The threshold here
        is loose on purpose — it is a smoke alarm for reintroduced per-line
        forking, not a benchmark.
        """
        recent = stamp(-60)
        self.write("".join("[{0}] [INFO] line {1}\n".format(recent, n) for n in range(20000)))
        started = time.time()
        rotate(self.path)
        self.assertLess(time.time() - started, 5.0)


class RotateConcurrencyTest(Sandbox):
    """Rotation runs on ThreadPoolExecutor workers, so it has to be safe there.

    The Logger is handed to Batch, Downloader, RcloneClient and Retrier, all of
    which log from pool threads, and rotation fires on roughly one call in a
    hundred. Two threads sharing one temp path could interleave their writes
    and then both rename over the log — and the log is what ``putioctl status``
    greps and the console renders.
    """

    def test_the_temp_path_is_per_thread(self):
        """Observed from inside ``rotate``, not reconstructed here: the point is
        what the code chooses, and a test that rebuilds the format string would
        pass even after the thread id was dropped from it."""
        import threading

        seen = []
        original = logs._rotate_locked

        def spy(log_file, cutoff, temp_path):
            seen.append(temp_path)
            return original(log_file, cutoff, temp_path)

        logs._rotate_locked = spy
        self.addCleanup(setattr, logs, "_rotate_locked", original)

        self.write("[{0}] [INFO] recent\n".format(stamp(-60)))
        # Keep all four threads alive until every one has rotated. A thread id
        # is only unique among live threads; once one exits the next may be
        # given the same id, which would read as a collision that production
        # (where the pool's workers are all alive at once) can never hit.
        done = threading.Barrier(4, timeout=10)

        def worker():
            rotate(self.path)
            done.wait()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(seen), 4)
        self.assertEqual(
            len(set(seen)), 4, "temp names must not collide across threads: " + repr(seen)
        )

    def test_hammering_rotate_from_threads_keeps_the_log_intact(self):
        import threading

        recent = stamp(-60)
        expected = "".join(
            "[{0}] [INFO] line {1}\n".format(recent, n) for n in range(500)
        )
        self.write(expected)

        errors = []

        def hammer():
            try:
                for _ in range(20):
                    rotate(self.path)
            except Exception as exc:  # pragma: no cover - a race would land here
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        # Every line is recent, so rotation is a no-op and the file must come
        # out byte-identical however many times it was rewritten.
        self.assertEqual(self.read().decode(), expected)
        # And nothing is left lying beside it.
        self.assertEqual(os.listdir(self.dir), ["processor.log"])


class DurationTest(unittest.TestCase):
    def test_the_summary_line_renderings(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(59), "59s")
        self.assertEqual(format_duration(60), "1m00s")
        self.assertEqual(format_duration(412), "6m52s")
        self.assertEqual(format_duration(3600), "1h00m")
        self.assertEqual(format_duration(7860), "2h11m")


if __name__ == "__main__":
    unittest.main()
