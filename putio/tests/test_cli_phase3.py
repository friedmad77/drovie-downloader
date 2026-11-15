"""The phase 3 wire format — what bash actually reads back.

The modules are tested elsewhere; this file tests the *handoff*, which is where
a port goes wrong quietly.  Three things it pins:

* the record shape, including empty fields, because the whole reason the
  separator is US and not a tab is that a tab would eat them;
* ``DONE``, which is what tells bash the subcommand ran;
* that a subcommand touching the filesystem touches only what it was asked to.

Filesystem effects run in a temporary directory, never against ``LOCAL_DIR``.
Nothing here opens a socket: the one command that would is driven through
:class:`~putio.tests.fake_http.FakeOpener`.
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
from putio.tests.fake_http import FakeOpener, FakeResponse, fixture

US = FIELD_SEP

#: See test_cli_network.py — the real RATE_LIMIT_FILE default is the live
#: window on this machine, and the shipped delay is a real second per search.
_RATE_DIR = tempfile.mkdtemp(prefix="putio-test-rate3-")

BASE_ENV = {
    "LOCAL_DIR": "/tmp/putio-test",
    "MOVIE_LIBRARY": "/library/Movies",
    "TV_LIBRARY": "/library/TV",
    "UNMATCHED_DIR": "/library/Unmatched",
    "TMDB_API_KEY": "test-key",
    "MAX_RETRIES": "1",
    "RETRY_DELAY": "0",
    "RATE_LIMIT_FILE": os.path.join(_RATE_DIR, "window"),
    "RATE_LIMIT_DELAY": "0",
}


class _Stdout:
    """A stdout with a ``.buffer``, which is what the record writer uses."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def value(self) -> str:
        return self.buffer.getvalue().decode("utf-8", "surrogateescape")


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def run(argv, env=None, stdin=b"", outcomes=None):
    """Run a subcommand in-process and return (code, stdout text, stderr)."""
    merged = dict(BASE_ENV)
    merged.update(env or {})
    out, err = _Stdout(), io.StringIO()
    opener = FakeOpener(outcomes if outcomes is not None else FakeResponse(b"{}"))
    with mock.patch.dict(os.environ, merged, clear=True):
        with mock.patch("urllib.request.urlopen", opener):
            with mock.patch("sys.stdin", _Stdin(stdin)):
                with mock.patch("sys.stdout", out):
                    with redirect_stderr(err):
                        code = main(argv)
    return code, out.value(), err.getvalue()


def records(text):
    return [line.split(US) for line in text.splitlines() if line]


def only(text, tag):
    return [row for row in records(text) if row[0] == tag]


# ---------------------------------------------------------------------------
# organize-plan
# ---------------------------------------------------------------------------


class OrganizePlan(unittest.TestCase):
    def test_a_match_produces_one_plan_record_and_done(self):
        code, out, _ = run(
            ["organize-plan"],
            stdin=b"Blade.Runner.2049.2017.1080p.mkv\n",
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
        )
        self.assertEqual(code, EXIT_OK)
        plans = only(out, "PLAN")
        self.assertEqual(len(plans), 1)
        self.assertEqual(records(out)[-1], ["DONE"])

    def test_the_record_has_exactly_nine_fields(self):
        # PLAN, name, action, dest, parse level, parse note, match level,
        # match note, dest name.  A reader splits on US into that many fields,
        # so a change here is a change to the contract.  dest_name was appended
        # rather than inserted so every earlier field kept its index.
        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Blade.Runner.2049.2017.1080p.mkv\n",
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
        )
        row = only(out, "PLAN")[0]
        self.assertEqual(len(row), 9)
        self.assertEqual(row[8], "Blade Runner 2049 (2017).mkv")

    def test_empty_fields_survive_the_separator(self):
        # This is the whole reason the separator is US.  An unparseable name
        # has an empty dest, an empty match level and an empty match note; with
        # a tab, bash's `read` would collapse them and shift every field left.
        _, out, _ = run(["organize-plan"], stdin=b"no-year-here.mkv\n")
        row = only(out, "PLAN")[0]
        self.assertEqual(len(row), 9)
        self.assertEqual(row[2], "unparsed")
        self.assertEqual(row[3], "")
        self.assertEqual(row[6], "")
        self.assertEqual(row[7], "")
        # A name that never matched has no canonical name to offer, and the
        # trailing empty field is the one most at risk of being eaten.
        self.assertEqual(row[8], "")

    def test_the_destination_is_built_from_the_configured_libraries(self):
        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Blade.Runner.2049.2017.1080p.mkv\n",
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
        )
        self.assertTrue(only(out, "PLAN")[0][3].startswith("/library/Movies/"))

    def test_a_tv_name_lands_in_a_season_directory(self):
        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Severance.S01E03.1080p.mkv\n",
            outcomes=FakeResponse(fixture("tmdb_tv_search.json")),
        )
        # The fixture is a TV search response, but organize-plan chooses the
        # endpoint from the parsed name — which is the point of the check.
        row = only(out, "PLAN")[0]
        self.assertEqual(row[2], "tv")
        self.assertTrue(row[3].endswith("/S01"), row[3])

    def test_no_results_is_unmatched_not_defer(self):
        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Nothing.2001.1080p.mkv\n",
            outcomes=FakeResponse(fixture("tmdb_movie_no_results.json")),
        )
        self.assertEqual(only(out, "PLAN")[0][2], "unmatched")

    def test_a_refused_key_is_defer_and_says_so(self):
        from putio.tests.fake_http import http_error

        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Nothing.2001.1080p.mkv\n",
            outcomes=http_error(
                "https://api.themoviedb.org/3/search/movie?api_key=k",
                401,
                fixture("tmdb_invalid_key.json"),
            ),
        )
        row = only(out, "PLAN")[0]
        self.assertEqual(row[2], "defer")
        self.assertEqual(row[6], "ERROR")
        self.assertIn("401", row[7])

    def test_the_api_key_never_reaches_a_record(self):
        from putio.tests.fake_http import http_error

        _, out, err = run(
            ["organize-plan"],
            env={"TMDB_API_KEY": "s3cr3tkey"},
            stdin=b"Nothing.2001.1080p.mkv\n",
            outcomes=http_error(
                "https://api.themoviedb.org/3/search/movie?api_key=s3cr3tkey&query=x",
                401,
                b'{"status_message":"Invalid API key"}',
            ),
        )
        self.assertNotIn("s3cr3tkey", out + err)

    def test_no_api_key_means_no_done_so_bash_does_the_whole_batch(self):
        code, out, _ = run(
            ["organize-plan"], env={"TMDB_API_KEY": ""}, stdin=b"x.2001.mkv\n"
        )
        self.assertEqual(code, EXIT_FAIL)
        self.assertNotIn("DONE", out)

    def test_an_empty_batch_is_still_done(self):
        code, out, _ = run(["organize-plan"], stdin=b"")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(records(out), [["DONE"]])

    def test_a_name_the_shell_could_not_carry_is_dropped_not_mangled(self):
        code, out, _ = run(
            ["organize-plan"],
            stdin=("we" + US + "ird.2001.mkv\n").encode(),
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(only(out, "PLAN"), [])
        self.assertTrue(any("could not be planned" in row[-1] for row in only(out, "LOG")))
        self.assertIn(["DONE"], records(out))

    def test_a_message_with_a_newline_cannot_split_a_record(self):
        from putio.tests.fake_http import http_error

        _, out, _ = run(
            ["organize-plan"],
            stdin=b"Nothing.2001.1080p.mkv\n",
            outcomes=http_error(
                "https://api.themoviedb.org/3/search/movie",
                500,
                b"<title>bad\ngateway</title>",
            ),
        )
        for row in only(out, "PLAN"):
            self.assertEqual(len(row), 9)


# ---------------------------------------------------------------------------
# the filesystem subcommands
# ---------------------------------------------------------------------------


class Sandboxed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="putio-test-arch-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def touch(self, *names):
        for name in names:
            open(os.path.join(self.dir, name), "w").close()

    def names(self):
        return sorted(os.listdir(self.dir))

    def run_here(self, argv, stdin=b""):
        return run(argv + ["--local-dir", self.dir], stdin=stdin)


class MarkersSweep(Sandboxed):
    def test_only_the_orphan_is_removed(self):
        self.touch(".extracted_Gone", ".extracted_Here", "Here.rar", "Movie.mkv")
        code, out, _ = self.run_here(["markers-sweep"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(["DONE"], records(out))
        self.assertEqual(self.names(), [".extracted_Here", "Here.rar", "Movie.mkv"])

    def test_it_says_how_many_of_how_many(self):
        self.touch(".extracted_Gone", ".extracted_Here", "Here.zip")
        _, out, _ = self.run_here(["markers-sweep"])
        messages = [row[2] for row in only(out, "LOG")]
        self.assertIn("[MARKER] Cleaned up 1 of 2 extraction markers", messages)

    def test_an_empty_directory_reports_nothing_and_is_still_done(self):
        _, out, _ = self.run_here(["markers-sweep"])
        self.assertEqual(only(out, "LOG"), [])
        self.assertIn(["DONE"], records(out))

    def test_an_unreadable_directory_is_not_a_failure(self):
        # A dropped SMB share.  Bash's glob would match nothing and sweep
        # nothing; this must not claim a clean sweep of a directory it could
        # not read, and must not fail the run either.
        code, out, _ = run(["markers-sweep", "--local-dir", self.dir + "/gone"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(["DONE"], records(out))


class ArchiveCheck(Sandboxed):
    def fields(self, out):
        return only(out, "CHECK")[0]

    def test_an_unmarked_complete_archive(self):
        self.touch("Movie.rar")
        code, out, _ = self.run_here(["archive-check", "Movie.rar"])
        self.assertEqual(code, EXIT_OK)
        row = self.fields(out)
        self.assertEqual(row[1], ".extracted_Movie")
        self.assertEqual(row[2], "0")  # not extracted
        self.assertEqual(row[3], "1")  # complete

    def test_a_marked_archive(self):
        self.touch("Movie.rar", ".extracted_Movie")
        _, out, _ = self.run_here(["archive-check", "Movie.rar"])
        self.assertEqual(self.fields(out)[2], "1")

    def test_a_marked_archive_is_never_reported_incomplete(self):
        # Its parts have been cleaned up; bash short-circuits before the
        # completeness check and so does this, or every already-extracted set
        # would start warning about itself.
        self.touch(".extracted_Movie", "Movie.rar")
        _, out, _ = self.run_here(["archive-check", "Movie.rar"])
        self.assertEqual(self.fields(out)[3], "1")
        self.assertEqual([r for r in only(out, "LOG") if "[RAR]" in r[2]], [])

    def test_a_two_volume_set_is_complete(self):
        self.touch("Movie.rar", "Movie.r00")
        _, out, _ = self.run_here(["archive-check", "Movie.rar"])
        self.assertEqual(self.fields(out)[3], "1")

    def test_a_missing_first_volume_is_incomplete_and_named(self):
        self.touch("Movie.rar", "Movie.r01")
        _, out, _ = self.run_here(["archive-check", "Movie.r00"])
        row = self.fields(out)
        self.assertEqual(row[3], "0")
        self.assertEqual(row[5], "Movie.r00")
        self.assertTrue(any("[RAR] Missing essential part" in r[2] for r in only(out, "LOG")))

    def test_nothing_on_disk_is_touched(self):
        self.touch("Movie.rar", "Movie.r00", ".extracted_Movie")
        before = self.names()
        self.run_here(["archive-check", "Movie.rar"])
        self.assertEqual(self.names(), before)


class ArchiveParts(Sandboxed):
    def test_local_targets_and_putio_parts(self):
        self.touch("Movie.rar", "Movie.r00", "Movie.r01", "Other.mkv")
        listing = b"Movie.rar\nMovie.r00\nMovie.r01\nOther.mkv\n"
        code, out, _ = self.run_here(["archive-parts", "Movie.rar"], stdin=listing)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            sorted(row[1] for row in only(out, "RM")),
            ["Movie.r00", "Movie.r01", "Movie.rar"],
        )
        self.assertEqual(
            [row[1] for row in only(out, "PART")],
            ["Movie.rar", "Movie.r00", "Movie.r01"],
        )
        self.assertEqual(only(out, "BASE")[0][1], "Movie")

    def test_a_zip_reports_an_empty_base_so_bash_deletes_one_file(self):
        self.touch("Movie.zip")
        _, out, _ = self.run_here(["archive-parts", "Movie.zip"], stdin=b"Movie.zip\n")
        self.assertEqual(only(out, "BASE")[0][1], "")
        self.assertEqual(only(out, "PART"), [])
        self.assertEqual([row[1] for row in only(out, "RM")], ["Movie.zip"])

    def test_an_unrelated_archive_is_not_a_part(self):
        # The bash test interpolates the base name into a regex unescaped, so
        # its dots are wildcards; MyXMovieX2019.r01 matches and is deleted.
        self.touch("My.Movie.2019.rar")
        listing = b"My.Movie.2019.rar\nMyXMovieX2019.r01\n"
        _, out, _ = self.run_here(["archive-parts", "My.Movie.2019"], stdin=listing)
        self.assertEqual([row[1] for row in only(out, "PART")], [])

    def test_nothing_on_disk_is_touched(self):
        self.touch("Movie.rar", "Movie.r00")
        before = self.names()
        self.run_here(["archive-parts", "Movie.rar"], stdin=b"Movie.rar\n")
        self.assertEqual(self.names(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
