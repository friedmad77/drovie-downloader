"""One transfer, in a sandbox — the disk check and both download paths.

No network and no rclone: the copy is a stub that writes the bytes it was asked
for, so what is under test is the sequence around it — where the file has to be
for the transfer to count as landed, when the put.io copy may be released, and
what the log says when it does not.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from putio.config import Config
from putio.proc import CommandResult
from putio.progress import ProgressPublisher
from putio.transfer import Downloader, _short_size, free_bytes


class FakeRclone:
    """rclone, if rclone were a dict and a file copy."""

    def __init__(self, sizes, fail=False, produce=True):
        self._sizes = sizes
        self.fail = fail
        self.produce = produce
        self.copies = []

    def size_of(self, remote_file):
        return self._sizes.get(remote_file)

    def sizes(self):
        return dict(self._sizes)

    def copy(self, remote_file, destination, log_path, inplace):
        self.copies.append((remote_file, destination, inplace))
        if self.fail:
            return CommandResult([], 1, "", "")
        if self.produce:
            name = os.path.basename(remote_file)
            with open(os.path.join(destination, name), "wb") as handle:
                handle.write(b"x" * self._sizes.get(remote_file, 1))
        return CommandResult([], 0, "", "")


class TransferCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="putio-transfer-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.local = os.path.join(self.root, "local")
        self.staging = os.path.join(self.root, "staging")
        self.progress_dir = os.path.join(self.root, "progress")
        os.makedirs(self.local)
        os.makedirs(self.staging)
        self.lines = []
        self.completed = []

    def build(self, sizes, use_temp_dir=False, fail=False, produce=True, **overrides):
        settings = dict(
            local_dir=self.local,
            staging_dir=self.staging,
            use_temp_dir=use_temp_dir,
            min_free_space_gb=0,
            max_retries=3,
        )
        settings.update(overrides)
        config = Config(**settings)
        rclone = FakeRclone(sizes, fail=fail, produce=produce)
        publisher = ProgressPublisher(self.progress_dir, "555")
        downloader = Downloader(
            config,
            rclone,
            publisher,
            lambda level, message: self.lines.append((level, message)),
            on_complete=self.completed.append,
        )
        return downloader, rclone, publisher

    def messages(self):
        return [message for _level, message in self.lines]


class DiskSpaceTest(TransferCase):
    def test_a_transfer_that_would_breach_the_buffer_is_refused(self):
        downloader, _rclone, _p = self.build({}, min_free_space_gb=10 ** 6)
        self.assertFalse(downloader.check_disk_space(1, "Film.mkv"))
        self.assertIn("[DISK] Insufficient disk space for Film.mkv", self.messages())

    def test_an_unmeasurable_volume_proceeds_rather_than_blocking(self):
        downloader, _rclone, _p = self.build({}, local_dir="/nonexistent/volume")
        self.assertTrue(downloader.check_disk_space(1, "Film.mkv"))
        self.assertIn(
            "[DISK] Could not determine available disk space, proceeding anyway",
            self.messages(),
        )

    def test_the_staging_volume_is_the_one_checked_when_staging_is_on(self):
        """Bytes land there first, so that is the volume that has to fit."""
        downloader, _rclone, _p = self.build({}, use_temp_dir=True)
        self.assertEqual(downloader.staging_root, self.staging)

    def test_free_bytes_is_available_not_total(self):
        free = free_bytes(self.root)
        self.assertIsNotNone(free)
        self.assertGreater(free, 0)


class DirectPathTest(TransferCase):
    def test_a_file_that_lands_counts_as_downloaded(self):
        downloader, rclone, _p = self.build({"Film.mkv": 8})
        self.assertTrue(downloader.fetch("Film.mkv", self.local))
        self.assertTrue(os.path.isfile(os.path.join(self.local, "Film.mkv")))
        self.assertEqual(self.completed, ["Film.mkv"])
        self.assertEqual(rclone.copies[0][2], True)  # --inplace

    def test_a_transfer_that_produced_nothing_is_a_failure(self):
        """rclone can exit 0 having copied nothing.  Bash checks the file is
        there afterwards and so does this — a "success" with no file would
        release the put.io copy."""
        downloader, _rclone, _p = self.build({"Film.mkv": 8}, produce=False)
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertTrue(
            any(m.startswith("[DOWNLOAD] File not found after download:") for m in self.messages())
        )
        self.assertEqual(self.completed, [])

    def test_a_failed_transfer_names_the_attempt_count(self):
        downloader, _rclone, _p = self.build({"Film.mkv": 8}, fail=True)
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertIn(
            "[DOWNLOAD] Failed to download Film.mkv after 3 attempts", self.messages()
        )

    def test_an_unknown_size_stops_before_anything_is_attempted(self):
        downloader, rclone, _p = self.build({})
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertIn("[DOWNLOAD] Could not determine size for Film.mkv", self.messages())
        self.assertEqual(rclone.copies, [])

    def test_a_zero_byte_file_is_refused_as_invalid(self):
        downloader, rclone, _p = self.build({"Film.mkv": 0})
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertIn("[DOWNLOAD] Invalid size for Film.mkv", self.messages())
        self.assertEqual(rclone.copies, [])


class StagedPathTest(TransferCase):
    def test_the_file_reaches_the_share_and_staging_is_left_empty(self):
        downloader, rclone, _p = self.build({"Film.mkv": 8}, use_temp_dir=True)
        self.assertTrue(downloader.fetch("Film.mkv", self.local))
        self.assertTrue(os.path.isfile(os.path.join(self.local, "Film.mkv")))
        self.assertEqual(os.listdir(self.staging), [])
        self.assertEqual(rclone.copies[0][2], False)  # no --inplace when staging

    def test_the_staging_directory_is_named_after_the_run(self):
        """``staging_sweep`` reads the pid out of ``dl_<pid>_XXXXXX`` and checks
        it against boot time; a name it cannot parse is a full-size media file
        nothing ever reclaims."""
        seen = {}

        downloader, rclone, _p = self.build({"Film.mkv": 8}, use_temp_dir=True)
        original = rclone.copy

        def spy(remote_file, destination, log_path, inplace):
            seen["dir"] = os.path.basename(destination)
            return original(remote_file, destination, log_path, inplace)

        rclone.copy = spy
        downloader.fetch("Film.mkv", self.local)
        self.assertTrue(seen["dir"].startswith("dl_555_"))

    def test_a_failed_transfer_removes_its_staging_directory(self):
        downloader, _rclone, _p = self.build({"Film.mkv": 8}, use_temp_dir=True, fail=True)
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertEqual(os.listdir(self.staging), [])

    def test_a_transfer_that_produced_nothing_removes_it_too(self):
        downloader, _rclone, _p = self.build(
            {"Film.mkv": 8}, use_temp_dir=True, produce=False
        )
        self.assertFalse(downloader.fetch("Film.mkv", self.local))
        self.assertEqual(os.listdir(self.staging), [])
        self.assertTrue(
            any(m.startswith("[DOWNLOAD] File not found in temp directory:") for m in self.messages())
        )

    def test_the_copy_phase_is_published_before_it_starts(self):
        """The staged copy is a plain copy that reports nothing, so readers
        fall back to measuring the destination — which only works if the meta
        file says so."""
        phases = []
        downloader, rclone, _p = self.build({"Film.mkv": 8}, use_temp_dir=True)
        original = rclone.copy

        def spy(remote_file, destination, log_path, inplace):
            result = original(remote_file, destination, log_path, inplace)
            for name in os.listdir(self.progress_dir):
                if name.endswith(".meta"):
                    with open(os.path.join(self.progress_dir, name)) as handle:
                        phases.append(handle.read())
            return result

        rclone.copy = spy
        downloader.fetch("Film.mkv", self.local)
        self.assertTrue(any("PHASE=download" in text for text in phases))

    def test_the_progress_files_are_gone_afterwards(self):
        downloader, _rclone, _p = self.build({"Film.mkv": 8}, use_temp_dir=True)
        downloader.fetch("Film.mkv", self.local)
        self.assertEqual(os.listdir(self.progress_dir), [])


class ShortSizeTest(unittest.TestCase):
    def test_it_reads_like_ls_lh(self):
        self.assertEqual(_short_size(512), "512B")
        self.assertEqual(_short_size(1536), "1.5K")
        self.assertEqual(_short_size(8461397504), "7.9G")


if __name__ == "__main__":
    unittest.main()
