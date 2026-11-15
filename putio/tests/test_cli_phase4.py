"""The ``download-batch`` wire format, and the fallback contract behind it.

The modules are tested elsewhere; what is pinned here is the handoff, which is
where a port goes wrong quietly.  For this subcommand that matters more than
for any other, because it is the only one whose failure mode is *doing the work
twice*: bash must be able to tell "never started" from "started and died", and
only the first of those may be re-run from a listing that is now stale.

Nothing here opens a socket or shells out to rclone; the batch is driven with
names that never reach TMDb and a remote that answers from a dict.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from putio.__main__ import EXIT_FAIL, EXIT_OK, FIELD_SEP, main
from putio.proc import CommandResult

US = FIELD_SEP


class _Stdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def value(self):
        return self.buffer.getvalue().decode("utf-8", "surrogateescape")


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def records(text):
    return [line.split(US) for line in text.splitlines() if line]


def tags(text):
    return [row[0] for row in records(text)]


def only(text, tag):
    return [row for row in records(text) if row[0] == tag]


class BatchCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="putio-cli4-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("local", "movies", "tv", "unmatched", "progress"):
            os.makedirs(os.path.join(self.root, name))
        self.env = {
            "LOCAL_DIR": os.path.join(self.root, "local"),
            "MOVIE_LIBRARY": os.path.join(self.root, "movies"),
            "TV_LIBRARY": os.path.join(self.root, "tv"),
            "UNMATCHED_DIR": os.path.join(self.root, "unmatched"),
            "PROGRESS_STATE_DIR": os.path.join(self.root, "progress"),
            "REMOTE": "putio:",
            "RCLONE_CMD": "/bin/echo",
            "TMDB_API_KEY": "test-key",
            "MAX_RETRIES": "1",
            "RETRY_DELAY": "0",
            "RUN_PID": "9191",
            "MEDIA_MOUNT_POINT": "",
            "DELETE_AFTER_DOWNLOAD": "true",
        }
        self.rclone_calls = []

    def run_batch(self, listing, sizes=None, env=None, runner=None):
        """Drive the subcommand with a fake rclone."""
        import json

        sizes = sizes if sizes is not None else {}
        local = self.env["LOCAL_DIR"]

        def fake_run(argv, **kwargs):
            self.rclone_calls.append(list(argv))
            if "lsjson" in argv:
                payload = [
                    {"Path": path, "Size": size, "IsDir": False}
                    for path, size in sizes.items()
                ]
                return CommandResult(argv, 0, json.dumps(payload), "")
            if "copy" in argv:
                # argv[-2] is "putio:<path>" — the remote prefix has no slash,
                # so basename() alone would keep it.
                name = os.path.basename(argv[-2].split(":", 1)[-1])
                with open(os.path.join(argv[-1], name), "wb") as handle:
                    handle.write(b"x" * 4096)
                return CommandResult(argv, 0, "", "")
            if "lsf" in argv:
                return CommandResult(argv, 0, "\n".join(sorted(sizes)), "")
            return CommandResult(argv, 0, "", "")

        merged = dict(self.env)
        merged.update(env or {})
        out, err = _Stdout(), io.StringIO()
        data = ("\n".join(listing) + "\n" if listing else "").encode()
        with mock.patch.dict(os.environ, merged, clear=True):
            # Three names, one function: RcloneClient calls ``run`` directly
            # for reads and through the Retrier — which lives in putio.proc —
            # for anything retried, and transfer.py shells out for numfmt.
            with mock.patch("putio.rclone.run", runner or fake_run):
                with mock.patch("putio.proc.run", runner or fake_run):
                    with mock.patch("putio.transfer.run", runner or fake_run):
                        with mock.patch("sys.stdin", _Stdin(data)):
                            with mock.patch("sys.stdout", out):
                                with redirect_stderr(err):
                                    code = main(["download-batch"])
        return code, out.value(), err.getvalue()


class ContractTest(BatchCase):
    def test_an_empty_listing_still_starts_and_finishes(self):
        code, out, _ = self.run_batch([])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(tags(out)[0], "START")
        self.assertEqual(tags(out)[-1], "DONE")

    def test_local_is_reported_before_any_transfer(self):
        """bash sets LOCAL_EXISTING_COUNT from it, and the run summary reads
        that to say "N local file(s)"."""
        code, out, _ = self.run_batch([])
        self.assertEqual(only(out, "LOCAL"), [["LOCAL", "0"]])

    def test_a_missing_tmdb_key_refuses_BEFORE_start(self):
        """The whole point of START: a refusal that emits it would tell bash
        not to fall back, and nothing would run at all."""
        code, out, err = self.run_batch([], env={"TMDB_API_KEY": ""})
        self.assertEqual(code, EXIT_FAIL)
        self.assertNotIn("START", tags(out))
        self.assertNotIn("DONE", tags(out))
        self.assertIn("TMDB_API_KEY", err)

    def test_a_missing_local_dir_refuses_before_start(self):
        code, out, _ = self.run_batch([], env={"LOCAL_DIR": ""})
        self.assertEqual(code, EXIT_FAIL)
        self.assertEqual(out, "")

    def test_a_missing_rclone_pin_refuses_before_start(self):
        code, out, _ = self.run_batch([], env={"RCLONE_CMD": ""})
        self.assertEqual(code, EXIT_FAIL)
        self.assertEqual(out, "")

    def test_an_error_after_start_still_ends_in_done(self):
        """Because bash must not re-run a batch that has already deleted things
        from put.io.  "Ran and some of it failed" is an ordinary run."""

        def exploding(argv, **kwargs):
            raise OSError("the world ended")

        code, out, _ = self.run_batch(["a.mkv"], runner=exploding)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("START", tags(out))
        self.assertEqual(tags(out)[-1], "DONE")
        self.assertTrue(
            any("[PARALLEL] Batch failed" in row[-1] for row in only(out, "LOG"))
        )

    def test_no_progress_file_survives_a_batch_that_blew_up(self):
        def exploding(argv, **kwargs):
            raise OSError("the world ended")

        self.run_batch(["a.mkv"], runner=exploding)
        self.assertEqual(os.listdir(self.env["PROGRESS_STATE_DIR"]), [])


class RecordTest(BatchCase):
    def test_a_downloaded_file_is_reported_by_name(self):
        code, out, _ = self.run_batch(
            ["no-year-here.mkv"], sizes={"no-year-here.mkv": 10 ** 9}
        )
        self.assertEqual(only(out, "DOWNLOADED"), [["DOWNLOADED", "no-year-here.mkv"]])

    def test_an_unparseable_name_lands_in_unmatched_and_adds_nothing(self):
        """It never reaches TMDb, which is what lets the whole round trip be
        checked without a network."""
        _code, out, _ = self.run_batch(
            ["no-year-here.mkv"], sizes={"no-year-here.mkv": 10 ** 9}
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "unmatched", "no-year-here.mkv"))
        )
        self.assertEqual(only(out, "ADDED"), [])

    def test_the_putio_copy_is_deleted_once_the_file_has_left_staging(self):
        self.run_batch(["no-year-here.mkv"], sizes={"no-year-here.mkv": 10 ** 9})
        deletes = [c for c in self.rclone_calls if "delete" in c]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][-1], "putio:no-year-here.mkv")

    def test_a_junk_name_is_deleted_and_never_downloaded(self):
        self.run_batch(["Sample.mkv"], sizes={"Sample.mkv": 10 ** 9})
        self.assertFalse([c for c in self.rclone_calls if "copy" in c])
        self.assertTrue([c for c in self.rclone_calls if "delete" in c])

    def test_log_records_carry_a_level_and_a_message(self):
        _code, out, _ = self.run_batch(
            ["no-year-here.mkv"], sizes={"no-year-here.mkv": 10 ** 9}
        )
        for row in only(out, "LOG"):
            self.assertEqual(len(row), 3)
            self.assertIn(row[1], ("DEBUG", "INFO", "WARN", "ERROR"))

    def test_a_newline_in_a_message_cannot_desynchronise_the_reader(self):
        _code, out, _ = self.run_batch([])
        for line in out.splitlines():
            self.assertNotIn("\n", line)


class ProgressTest(BatchCase):
    def test_transfers_are_published_under_the_runs_pid(self):
        seen = {}
        import json

        def watching(argv, **kwargs):
            self.rclone_calls.append(list(argv))
            if "lsjson" in argv:
                return CommandResult(
                    argv,
                    0,
                    json.dumps([{"Path": "no-year-here.mkv", "Size": 10 ** 9}]),
                    "",
                )
            if "copy" in argv:
                seen["files"] = sorted(os.listdir(self.env["PROGRESS_STATE_DIR"]))
                with open(os.path.join(argv[-1], "no-year-here.mkv"), "wb") as handle:
                    handle.write(b"x")
                return CommandResult(argv, 0, "", "")
            return CommandResult(argv, 0, "", "")

        self.run_batch(["no-year-here.mkv"], runner=watching)
        self.assertEqual(seen["files"], ["dl.9191.1.log", "dl.9191.1.meta"])
        self.assertEqual(os.listdir(self.env["PROGRESS_STATE_DIR"]), [])


if __name__ == "__main__":
    unittest.main()
