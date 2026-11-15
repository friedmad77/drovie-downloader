"""The per-file share gate — three answers, and which one aborts the queue."""

from __future__ import annotations

import unittest

from putio.config import Config
from putio.mount import ALIVE, BUSY, DEAD, MountGate


class GateTest(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def gate(self, status, mount_point="/Volumes/Media"):
        config = Config(media_mount_point=mount_point)
        return MountGate(
            config,
            "/opt/drovie-downloader/putio_download_and_delete.sh",
            lambda level, message: self.lines.append((level, message)),
            runner=lambda: status,
        )

    def messages(self):
        return [message for _level, message in self.lines]

    def test_an_unset_mount_point_switches_the_feature_off(self):
        gate = self.gate(DEAD, mount_point="")
        self.assertFalse(gate.enabled)
        self.assertTrue(gate.check("Film.mkv"))
        self.assertEqual(self.lines, [])

    def test_a_live_share_says_nothing(self):
        self.assertTrue(self.gate(ALIVE).check("Film.mkv"))
        self.assertEqual(self.lines, [])

    def test_a_remount_in_progress_defers_the_file_without_condemning_the_share(self):
        """Returning plain failure here would let the queue declare the share
        dead seconds before it recovered."""
        gate = self.gate(BUSY)
        self.assertFalse(gate.check("Film.mkv"))
        self.assertFalse(gate.dead)
        self.assertIn(
            "[MOUNT] Deferring Film.mkv — a remount is still in progress", self.messages()
        )

    def test_a_dead_share_is_remembered_for_the_rest_of_the_run(self):
        """Bash keeps this in a file keyed on ``$$`` because the download jobs
        are subshells.  One process needs no file — and cannot leave one behind
        for a run that dies before its EXIT trap."""
        probes = {"n": 0}

        def runner():
            probes["n"] += 1
            return DEAD

        config = Config(media_mount_point="/Volumes/Media")
        gate = MountGate(
            config, "/x.sh", lambda level, message: self.lines.append((level, message)), runner=runner
        )
        self.assertFalse(gate.check("a.mkv"))
        self.assertFalse(gate.check("b.mkv"))
        self.assertEqual(probes["n"], 1, "the share is probed once, not once per file")
        self.assertIn(
            "[MOUNT] Aborting a.mkv — /Volumes/Media is unavailable", self.messages()
        )
        self.assertIn(
            "[MOUNT] Skipping b.mkv — share already declared unavailable this run",
            self.messages(),
        )

    def test_no_helper_path_lets_the_queue_run(self):
        """A missing helper must not block downloads: a share that really is
        gone still fails loudly at the move, which is where the run was before
        the gate existed."""
        config = Config(media_mount_point="/Volumes/Media")
        gate = MountGate(config, "", lambda level, message: None)
        self.assertTrue(gate.check("Film.mkv"))


if __name__ == "__main__":
    unittest.main()
