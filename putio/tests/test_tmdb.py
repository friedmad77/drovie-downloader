"""TMDb search, against responses recorded from the live service.

The bash these replace collapse three outcomes into one branch — no such film,
bad key, server error all end as "move it to Unmatched" — so the tests are
mostly about keeping those three apart while the *answers* stay byte-identical
to what ``jq`` produced.
"""

from __future__ import annotations

import unittest
import urllib.error

from putio.http import HttpClient, HttpStatusError, HttpTransportError
from putio.ratelimit import NullLimiter
from putio.tests.fake_http import FakeOpener, FakeResponse, RecordingSleep, fixture, http_error
from putio.tmdb import TmdbClient

KEY = "test-key-not-a-real-one"


def client(outcomes, limiter=None):
    opener = FakeOpener(outcomes)
    http = HttpClient(opener=opener, sleep=RecordingSleep(), max_retries=1)
    return TmdbClient(KEY, http, limiter or NullLimiter()), opener


class TestMovieSearch(unittest.TestCase):
    def test_the_first_result_is_taken_whole(self):
        """`.results[0].title` and `.release_date | cut -d- -f1`."""
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_search.json")))
        match = tmdb.search_movie("Blade Runner 2049", "2017")
        self.assertEqual(match.title, "Blade Runner 2049")
        self.assertEqual(match.year, "2017")

    def test_the_year_is_a_string_and_can_be_empty(self):
        """A film with no release_date is filed by bash as `Title ()`.

        Returning None here instead would make that directory `Title (None)`,
        which is the sort of thing nobody notices until the library has one.
        """
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_no_date.json")))
        match = tmdb.search_movie("Blade Runner 2049")
        self.assertEqual(match.year, "")

    def test_a_year_that_moved_is_reported_without_changing_the_answer(self):
        """The incident, recorded from the live service.

        ``The Good Boy 2025 …`` matched the 2026 film, correctly — but nothing
        said so, and the file then sat in a 2026 folder under a 2025 name until
        Plex matched a different film of the same title.
        """
        notes = []
        opener = FakeOpener(FakeResponse(fixture("tmdb_movie_ambiguous.json")))
        http = HttpClient(opener=opener, sleep=RecordingSleep(), max_retries=1)
        tmdb = TmdbClient(KEY, http, NullLimiter(), on_note=lambda l, m: notes.append((l, m)))

        match = tmdb.search_movie("The Good Boy", "2025")

        # The answer is untouched: still results[0], still TMDb's ordering.
        self.assertEqual(match.title, "Good Boy")
        self.assertEqual(match.year, "2026")
        self.assertEqual(match.tmdb_id, 1381027)

        self.assertEqual(len(notes), 1)
        level, message = notes[0]
        self.assertEqual(level, "WARN")
        self.assertIn("asked for 2025", message)
        self.assertIn("matched 2026", message)
        self.assertIn("tmdb 1381027", message)

    def test_the_years_agreeing_says_nothing_at_all(self):
        notes = []
        opener = FakeOpener(FakeResponse(fixture("tmdb_movie_search.json")))
        http = HttpClient(opener=opener, sleep=RecordingSleep(), max_retries=1)
        tmdb = TmdbClient(KEY, http, NullLimiter(), on_note=lambda l, m: notes.append((l, m)))
        tmdb.search_movie("Blade Runner 2049", "2017")
        self.assertEqual(notes, [])

    def test_no_year_asked_means_nothing_to_disagree_with(self):
        notes = []
        opener = FakeOpener(FakeResponse(fixture("tmdb_movie_ambiguous.json")))
        http = HttpClient(opener=opener, sleep=RecordingSleep(), max_retries=1)
        tmdb = TmdbClient(KEY, http, NullLimiter(), on_note=lambda l, m: notes.append((l, m)))
        tmdb.search_movie("The Good Boy")
        self.assertEqual(notes, [])

    def test_a_client_with_no_on_note_still_searches(self):
        # Every hand-run caller passes one, but the default must not crash.
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_ambiguous.json")))
        self.assertEqual(tmdb.search_movie("The Good Boy", "2025").year, "2026")

    def test_no_results_is_none_not_an_error(self):
        """TMDb genuinely not knowing a film is the Unmatched case, correctly."""
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_no_results.json")))
        self.assertIsNone(tmdb.search_movie("zzzzqqqqxxxxnotathing"))

    def test_a_revoked_key_raises_instead_of_looking_like_no_results(self):
        """The distinction the whole phase exists for.

        Under the bash both of the two tests above and this one produce the
        same log line and the same move to Unmatched.
        """
        tmdb, _ = client(http_error("https://x", 401, fixture("tmdb_invalid_key.json")))
        with self.assertRaises(HttpStatusError) as caught:
            tmdb.search_movie("Heat")
        self.assertIn("Invalid API key", str(caught.exception))

    def test_an_unreachable_tmdb_raises(self):
        tmdb, _ = client(urllib.error.URLError("no route to host"))
        with self.assertRaises(HttpTransportError):
            tmdb.search_movie("Heat")


class TestTvSearch(unittest.TestCase):
    def test_it_reads_name_and_first_air_date(self):
        """Different field names from the movie endpoint, easily transposed."""
        tmdb, _ = client(FakeResponse(fixture("tmdb_tv_search.json")))
        match = tmdb.search_tv("Severance", "2022")
        self.assertEqual(match.title, "Severance")
        self.assertEqual(match.year, "2022")

    def test_the_year_parameter_is_named_for_the_tv_endpoint(self):
        """`first_air_date_year`, not `year`.

        TMDb ignores an unknown parameter rather than rejecting it, so getting
        this wrong would silently widen every TV search — and the results would
        still look plausible.
        """
        tmdb, opener = client(FakeResponse(fixture("tmdb_tv_search.json")))
        tmdb.search_tv("Severance", "2022")
        self.assertIn("first_air_date_year=2022", opener.last.url)
        self.assertNotIn("&year=", opener.last.url)


class TestUrlConstruction(unittest.TestCase):
    def test_an_absent_year_is_not_sent(self):
        """Mirrors `if [ -n "$year" ]; then search_url="…&year=$year"; fi`."""
        tmdb, opener = client(FakeResponse(fixture("tmdb_movie_search.json")))
        tmdb.search_movie("Heat")
        self.assertNotIn("year=", opener.last.url)

    def test_the_query_is_encoded_as_jq_encoded_it(self):
        tmdb, opener = client(FakeResponse(fixture("tmdb_movie_search.json")))
        tmdb.search_movie("Blade Runner 2049")
        self.assertIn("query=Blade%20Runner%202049", opener.last.url)

    def test_the_endpoints_are_the_ones_the_bash_calls(self):
        tmdb, opener = client(FakeResponse(fixture("tmdb_movie_search.json")))
        tmdb.search_movie("Heat")
        self.assertTrue(opener.last.url.startswith("https://api.themoviedb.org/3/search/movie?"))
        tmdb.search_tv("Heat")
        self.assertTrue(opener.last.url.startswith("https://api.themoviedb.org/3/search/tv?"))


class TestNonUtf8Titles(unittest.TestCase):
    """The names phase 3 will feed in, and bash already files today."""

    def test_a_surrogate_escaped_title_reaches_tmdb_as_percent_escapes(self):
        tmdb, opener = client(FakeResponse(fixture("tmdb_movie_search.json")))
        tmdb.search_movie("latin1-caf\udce9", "2001")
        self.assertIn("query=latin1-caf%E9", opener.last.url)

    def test_it_fails_as_an_httperror_or_not_at_all(self):
        """Whatever goes wrong must be catchable by the caller's one except.

        A UnicodeEncodeError out of the URL builder would escape a per-item
        `except HttpError` and take every other title in the batch with it.
        """
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_no_results.json")))
        try:
            self.assertIsNone(tmdb.search_movie("\udcff\udcfe"))
        except HttpStatusError:
            pass  # an HttpError is a fine outcome; anything else is not


class TestRateLimiting(unittest.TestCase):
    def test_every_search_takes_the_limiter_first(self):
        """`rate_limit_tmdb` is called before the URL is built, both places."""

        class Counting:
            def __init__(self):
                self.n = 0

            def acquire(self):
                self.n += 1

        limiter = Counting()
        tmdb, _ = client(FakeResponse(fixture("tmdb_movie_search.json")), limiter)
        tmdb.search_movie("Heat")
        tmdb.search_tv("Heat")
        self.assertEqual(limiter.n, 2)

    def test_the_limiter_runs_even_when_the_search_then_fails(self):
        """Otherwise a run of failures would hammer TMDb at full speed —
        which is a good way to turn a transient 500 into a real ban."""

        class Counting:
            def __init__(self):
                self.n = 0

            def acquire(self):
                self.n += 1

        limiter = Counting()
        tmdb, _ = client(http_error("https://x", 401, b"{}"), limiter)
        with self.assertRaises(HttpStatusError):
            tmdb.search_movie("Heat")
        self.assertEqual(limiter.n, 1)


class TestMalformedResponses(unittest.TestCase):
    """A 200 with a shape nobody expected must not kill the run."""

    def test_results_missing_entirely(self):
        tmdb, _ = client(FakeResponse(b'{"page":1}'))
        self.assertIsNone(tmdb.search_movie("Heat"))

    def test_results_is_not_a_list(self):
        tmdb, _ = client(FakeResponse(b'{"results":"nope"}'))
        self.assertIsNone(tmdb.search_movie("Heat"))

    def test_a_result_with_a_null_title_is_no_match(self):
        """jq's `// empty` treats null and absent alike."""
        tmdb, _ = client(FakeResponse(b'{"results":[{"title":null,"release_date":"1995-01-01"}]}'))
        self.assertIsNone(tmdb.search_movie("Heat"))

    def test_a_top_level_list_instead_of_an_object(self):
        tmdb, _ = client(FakeResponse(b"[]"))
        self.assertIsNone(tmdb.search_movie("Heat"))


if __name__ == "__main__":
    unittest.main()
