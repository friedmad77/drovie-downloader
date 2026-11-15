"""``python3 -m putio`` — exit codes, output shape, and what it must not print."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from putio import SCHEMA, __version__
from putio.__main__ import EXIT_FAIL, EXIT_OK, EXIT_USAGE, main


def run(argv, env=None):
    """Run a subcommand in-process, returning (exit code, stdout, stderr)."""
    import os
    from unittest import mock

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=True):
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestVersion(unittest.TestCase):
    def test_version_is_json_carrying_the_schema(self):
        code, out, _ = run(["version"])
        self.assertEqual(code, EXIT_OK)
        data = json.loads(out)
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["schema"], SCHEMA)


class TestConfigDump(unittest.TestCase):
    def test_json_dump_parses(self):
        code, out, _ = run(["config-dump", "--json"], {"LOCAL_DIR": "/tmp/x"})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["local_dir"], "/tmp/x")

    def test_no_secret_reaches_stdout_in_either_format(self):
        env = {"LOCAL_DIR": "/tmp/x", "TMDB_API_KEY": "s3cr3t", "PLEX_TOKEN": "t0ken"}
        for argv in (["config-dump"], ["config-dump", "--json"]):
            code, out, err = run(argv, env)
            self.assertEqual(code, EXIT_OK)
            self.assertNotIn("s3cr3t", out + err, argv)
            self.assertNotIn("t0ken", out + err, argv)


class TestSelftest(unittest.TestCase):
    def test_passes_when_the_handoff_happened(self):
        code, out, _ = run(["selftest"], {"LOCAL_DIR": "/tmp/x"})
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ok:", out)

    def test_quiet_says_nothing_on_the_happy_path(self):
        _, out, _ = run(["selftest", "-q"], {"LOCAL_DIR": "/tmp/x", "REMOTE": "putio:"})
        self.assertNotIn("ok:", out)

    def test_fails_when_bash_did_not_hand_anything_over(self):
        """An empty LOCAL_DIR is the tell that putio_python was bypassed."""
        code, _, err = run(["selftest"], {})
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("LOCAL_DIR", err)


class TestUsage(unittest.TestCase):
    def test_no_subcommand_is_a_usage_error_not_a_crash(self):
        code, _, err = run([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_unknown_subcommand_exits_two(self):
        with self.assertRaises(SystemExit) as caught:
            run(["nonsense"])
        self.assertEqual(caught.exception.code, EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
