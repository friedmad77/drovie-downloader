"""The pause gate — the file ``putioctl pause`` writes, read the way bash read it.

Three things here are contract rather than implementation: the file's field
names, the fact that a paused run exits 0, and that the skip is logged once
per *pause* rather than once per cron tick.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from putio.pause import check_pause, read_pause


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-pause-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pause_file = os.path.join(self.dir, "paused")
        self.log_file = os.path.join(self.dir, "run.log")
        self.logged = []

    def log(self, level, message):
        self.logged.append((level, message))
        with open(self.log_file, "a") as handle:
            handle.write("[2026-08-24 00:00:00] [{0}] {1}\n".format(level, message))

    def pause(self, until=None, reason=None):
        lines = []
        if until is not None:
            lines.append('PAUSED_UNTIL="{0}"'.format(until))
        if reason is not None:
            lines.append('PAUSED_REASON="{0}"'.format(reason))
        with open(self.pause_file, "w") as handle:
            handle.write("\n".join(lines) + "\n")

    def check(self, env=None):
        return check_pause(self.pause_file, self.log_file, self.log, env or {})


class ReadTest(Sandbox):
    def test_no_file_is_no_pause(self):
        self.assertIsNone(read_pause(self.pause_file))

    def test_an_indefinite_pause(self):
        self.pause(until=0, reason="NAS is out")
        state = read_pause(self.pause_file)
        self.assertIsNone(state.until)
        self.assertEqual(state.reason, "NAS is out")
        self.assertEqual(state.describe(), "indefinitely")

    def test_a_timed_pause(self):
        later = int(time.time()) + 3600
        self.pause(until=later)
        state = read_pause(self.pause_file)
        self.assertEqual(state.until, later)
        self.assertFalse(state.expired)
        self.assertTrue(state.describe().startswith("until 20"))

    def test_a_lapsed_pause_reads_as_expired(self):
        self.pause(until=int(time.time()) - 1)
        self.assertTrue(read_pause(self.pause_file).expired)

    def test_a_non_numeric_until_is_indefinite(self):
        """``[[ "$pause_until" =~ ^[0-9]+$ ]]`` — a word, or a negative, means
        the pause has no expiry rather than one in the past."""
        self.pause(until="soon")
        self.assertIsNone(read_pause(self.pause_file).until)
        self.pause(until="-5")
        self.assertIsNone(read_pause(self.pause_file).until)

    def test_the_two_fields_are_unquoted_differently(self):
        """Bash's asymmetry, reproduced: ``PAUSED_UNTIL`` went through
        ``tr -d '"'`` (every quote anywhere) and ``PAUSED_REASON`` through
        ``sed 's/^"//; s/"$//'`` (one at each end)."""
        with open(self.pause_file, "w") as handle:
            handle.write('PAUSED_UNTIL="123"456"\nPAUSED_REASON="say "hi" now"\n')
        state = read_pause(self.pause_file)
        self.assertEqual(state.until, 123456)
        self.assertEqual(state.reason, 'say "hi" now')

    def test_an_unreadable_file_is_not_a_pause(self):
        """A pause nobody can read is a pause nobody can lift; the sync
        stopping forever is the worse failure."""
        os.mkdir(os.path.join(self.dir, "adir"))
        self.assertIsNone(read_pause(os.path.join(self.dir, "adir")))


class GateTest(Sandbox):
    def test_no_pause_lets_the_run_through(self):
        self.assertFalse(self.check())
        self.assertEqual(self.logged, [])

    def test_a_pause_stops_the_run_and_says_so_once(self):
        self.pause(until=0, reason="deploying")
        self.assertTrue(self.check())
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(
            self.logged[0],
            ("INFO", "[PAUSE] Sync is paused indefinitely (deploying) — skipping run"),
        )

    def test_the_next_tick_stops_silently(self):
        """Rotation only runs on runs that proceed, so a line every five
        minutes would grow the log for as long as the pause lasted with
        nothing ever trimming it."""
        self.pause(until=0)
        self.check()
        self.logged = []
        self.assertTrue(self.check())
        self.assertEqual(self.logged, [])

    def test_a_line_after_the_pause_note_makes_it_speak_again(self):
        """The check is ``tail -n 1``, so an intervening line means this is a
        fresh pause as far as the log is concerned."""
        self.pause(until=0)
        self.check()
        self.log("INFO", "something else happened")
        self.logged = []
        self.assertTrue(self.check())
        self.assertEqual(len(self.logged), 1)

    def test_a_reasonless_pause_omits_the_parenthetical(self):
        self.pause(until=0)
        self.check()
        self.assertEqual(
            self.logged[0][1], "[PAUSE] Sync is paused indefinitely — skipping run"
        )

    def test_an_expired_pause_clears_itself_and_the_run_proceeds(self):
        """What makes a timed pause resume without anyone remembering to run
        ``putioctl resume``."""
        self.pause(until=int(time.time()) - 1)
        self.assertFalse(self.check())
        self.assertFalse(os.path.exists(self.pause_file))
        self.assertEqual(
            self.logged, [("INFO", "[PAUSE] Pause expired — resuming scheduled runs")]
        )

    def test_ignore_pause_bypasses_the_gate_without_clearing_it(self):
        """``putioctl sync --force``.  The pause must survive the forced run."""
        self.pause(until=0, reason="held")
        self.assertFalse(self.check({"PUTIO_IGNORE_PAUSE": "1"}))
        self.assertTrue(os.path.exists(self.pause_file))
        self.assertEqual(self.logged, [])

    def test_any_other_value_of_ignore_pause_does_not_bypass(self):
        self.pause(until=0)
        self.assertTrue(self.check({"PUTIO_IGNORE_PAUSE": "0"}))
        self.assertTrue(self.check({"PUTIO_IGNORE_PAUSE": "yes"}))


if __name__ == "__main__":
    unittest.main()
