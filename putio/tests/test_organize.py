"""The organise plan: where each file goes, and what bash is told about it.

Three groups matter more than the rest:

* :class:`SeasonDirectories` pins the S08/S09 fix — bash's ``printf "%02d"``
  reads a leading zero as octal and files those two seasons into ``S00``.
* :class:`FailedLookups` pins the difference between "TMDb has nothing" and
  "TMDb refused", which bash cannot see at all.
* :class:`Transport` pins what happens to a name the shell handoff could not
  carry, because a *silently* wrong destination is the failure worth spending
  code to prevent.
"""

from __future__ import annotations

import unittest

from putio.http import HttpStatusError, HttpTransportError
from putio.organize import (
    DEFER,
    MOVIE,
    TV,
    UNMATCHED,
    UNPARSED,
    Decision,
    canonical_name,
    episode_code,
    plan,
    season_directory,
    unsafe_for_transport,
)
from putio.tmdb import Match

MOVIES = "/library/Movies"
SHOWS = "/library/TV"


class FakeTmdb:
    """Answers from a table, and counts what it was asked.

    Keyed on the query alone, not the year: every test here that cares about
    the year asserts on the recorded call instead.
    """

    def __init__(self, movies=None, shows=None, error=None):
        self.movies = movies or {}
        self.shows = shows or {}
        self.error = error
        self.calls = []

    def search_movie(self, query, year=""):
        self.calls.append(("movie", query, year))
        if self.error is not None:
            raise self.error
        return self.movies.get(query)

    def search_tv(self, query, year=""):
        self.calls.append(("tv", query, year))
        if self.error is not None:
            raise self.error
        return self.shows.get(query)


def one(name, tmdb):
    decisions = plan([name], MOVIES, SHOWS, tmdb)
    assert len(decisions) == 1
    return decisions[0]


class SeasonDirectories(unittest.TestCase):
    """DIVERGENCE — bash files S08 and S09 into Plex's Specials folder.

    ``printf "%02d" 08`` in bash 3.2 prints ``00`` and writes ``08: invalid
    number`` to stderr, because a leading zero is an octal prefix and ``08`` is
    not octal.  Seasons 1-7 and 10 upwards are unaffected, which is exactly why
    nobody noticed: the two that break are the two that look like the ones that
    work.  ``S00`` is not merely the wrong season either — Plex reads it as
    Specials.
    """

    def test_the_two_octal_seasons(self):
        self.assertEqual(season_directory("08"), "S08")
        self.assertEqual(season_directory("09"), "S09")

    def test_the_seasons_that_always_worked(self):
        self.assertEqual(season_directory("01"), "S01")
        self.assertEqual(season_directory("07"), "S07")
        self.assertEqual(season_directory("10"), "S10")
        self.assertEqual(season_directory("22"), "S22")

    def test_a_single_digit_is_padded(self):
        self.assertEqual(season_directory("1"), "S01")

    def test_it_is_total(self):
        # Unreachable from parse_media_info, which captures [0-9]{1,2} — but
        # this is behind a CLI, and a crash here would cost a whole batch.
        self.assertEqual(season_directory("x"), "Sx")


class Destinations(unittest.TestCase):
    def test_a_film_gets_title_and_year(self):
        tmdb = FakeTmdb(movies={"Blade Runner 2049": Match("Blade Runner 2049", "2017")})
        decision = one("Blade.Runner.2049.2017.1080p.mkv", tmdb)
        self.assertEqual(decision.action, MOVIE)
        self.assertEqual(decision.dest, MOVIES + "/Blade Runner 2049 (2017)")

    def test_a_show_gets_a_season_directory(self):
        tmdb = FakeTmdb(shows={"Severance": Match("Severance", "2022")})
        decision = one("Severance.S01E03.1080p.mkv", tmdb)
        self.assertEqual(decision.action, TV)
        self.assertEqual(decision.dest, SHOWS + "/Severance/S01")

    def test_the_tmdb_title_is_sanitized_not_the_parsed_one(self):
        tmdb = FakeTmdb(movies={"Star Wars": Match('Star Wars: "A New Hope"', "1977")})
        decision = one("Star.Wars.1977.1080p.mkv", tmdb)
        self.assertEqual(decision.dest, MOVIES + "/Star Wars— A New Hope (1977)")

    def test_a_film_with_no_release_date_is_filed_as_title_open_close(self):
        # QUIRK, reproduced: bash's `cut -d- -f1` on an empty date gives an
        # empty year and the directory really is called "Title ()".  1,814
        # directories were created under these rules; this one is not a bug to
        # fix in passing.
        tmdb = FakeTmdb(movies={"Untitled": Match("Untitled", "")})
        decision = one("Untitled.2001.1080p.mkv", tmdb)
        self.assertEqual(decision.dest, MOVIES + "/Untitled ()")

    def test_the_year_reaches_tmdb(self):
        tmdb = FakeTmdb(movies={"Hijack": Match("Hijack", "2023")})
        one("Hijack.2023.1080p.mkv", tmdb)
        self.assertEqual(tmdb.calls, [("movie", "Hijack", "2023")])


class LogLines(unittest.TestCase):
    """The words are bash's, so a run reads the same as it did before."""

    def test_the_parse_line(self):
        tmdb = FakeTmdb(shows={"Severance": Match("Severance", "2022")})
        decision = one("Severance.S01E03.1080p.mkv", tmdb)
        self.assertEqual(decision.parse_level, "INFO")
        self.assertEqual(
            decision.parse_note,
            "[PARSE] type='tv', title='Severance', year='', season='01', episode='03'",
        )

    def test_the_found_lines(self):
        tmdb = FakeTmdb(
            movies={"Hijack": Match("Hijack", "2023")},
            shows={"Severance": Match("Severance", "2022")},
        )
        self.assertEqual(
            one("Hijack.2023.1080p.mkv", tmdb).match_note,
            "[TMDB] Found movie: 'Hijack' (2023)",
        )
        self.assertEqual(
            one("Severance.S01E03.1080p.mkv", tmdb).match_note,
            "[TMDB] Found TV show: 'Severance' (2022)",
        )

    def test_an_unparseable_name_carries_bash_s_warning(self):
        decision = one("no-year-here.mkv", FakeTmdb())
        self.assertEqual(decision.action, UNPARSED)
        self.assertEqual(decision.parse_level, "WARN")
        self.assertIn("Could not parse media info from filename", decision.parse_note)
        self.assertEqual(decision.dest, "")


class FailedLookups(unittest.TestCase):
    """"TMDb has nothing" and "TMDb refused" are not the same answer.

    Bash cannot tell them apart: ``curl -s`` exits 0 on a 401 and ``jq`` reads
    no title out of the error body, so both end in ``UNMATCHED_DIR`` — and the
    put.io copy is then deleted, because the file did leave staging.  A revoked
    ``TMDB_API_KEY`` empties a whole run's downloads into Unmatched with
    nothing in the log naming the cause.
    """

    def test_no_results_still_means_unmatched(self):
        decision = one("Nothing.2001.1080p.mkv", FakeTmdb())
        self.assertEqual(decision.action, UNMATCHED)
        self.assertEqual(decision.match_level, "WARN")
        self.assertIn("Lookup failed for Nothing.2001.1080p.mkv", decision.match_note)
        self.assertIn("query: Nothing", decision.match_note)

    def test_no_results_for_tv_uses_bash_s_other_wording(self):
        decision = one("Nothing.S01E01.1080p.mkv", FakeTmdb())
        self.assertEqual(decision.action, UNMATCHED)
        self.assertIn("TV lookup failed for", decision.match_note)

    def test_a_refused_key_defers_instead_of_filing(self):
        error = HttpStatusError(401, "Unauthorized", "https://api.themoviedb.org/3/x", b"")
        decision = one("Hijack.2023.1080p.mkv", FakeTmdb(error=error))
        self.assertEqual(decision.action, DEFER)
        self.assertEqual(decision.dest, "")
        self.assertEqual(decision.match_level, "ERROR")
        self.assertIn("HTTP 401", decision.match_note)

    def test_a_transport_failure_defers_too(self):
        error = HttpTransportError("no route to host", "https://api.themoviedb.org/3/x")
        decision = one("Hijack.2023.1080p.mkv", FakeTmdb(error=error))
        self.assertEqual(decision.action, DEFER)
        self.assertIn("Failed to query TMDb for", decision.match_note)

    def test_the_api_key_cannot_reach_the_note(self):
        url = "https://api.themoviedb.org/3/search/movie?api_key=s3cr3t&query=x"
        error = HttpStatusError(401, "Unauthorized", url, b"")
        decision = one("Hijack.2023.1080p.mkv", FakeTmdb(error=error))
        self.assertNotIn("s3cr3t", decision.match_note)


class Batching(unittest.TestCase):
    """One lookup per distinct query, not one per file."""

    def test_a_season_pack_is_one_search(self):
        tmdb = FakeTmdb(shows={"Severance": Match("Severance", "2022")})
        names = ["Severance.S01E%02d.1080p.mkv" % n for n in range(1, 11)]
        decisions = plan(names, MOVIES, SHOWS, tmdb)
        self.assertEqual(len(decisions), 10)
        self.assertEqual(len(tmdb.calls), 1)

    def test_every_file_still_gets_its_own_lines_and_season(self):
        tmdb = FakeTmdb(shows={"Show": Match("Show", "2020")})
        decisions = plan(
            ["Show.S01E01.x.mkv", "Show.S08E02.x.mkv"], MOVIES, SHOWS, tmdb
        )
        self.assertEqual(decisions[0].dest, SHOWS + "/Show/S01")
        self.assertEqual(decisions[1].dest, SHOWS + "/Show/S08")
        self.assertEqual(decisions[0].match_note, decisions[1].match_note)

    def test_a_failure_is_cached_so_a_pack_pays_the_retry_ladder_once(self):
        error = HttpTransportError("down", "https://api.themoviedb.org/3/x")
        tmdb = FakeTmdb(error=error)
        names = ["Show.S01E%02d.x.mkv" % n for n in range(1, 6)]
        decisions = plan(names, MOVIES, SHOWS, tmdb)
        self.assertEqual(len(tmdb.calls), 1)
        self.assertTrue(all(d.action == DEFER for d in decisions))

    def test_a_year_difference_is_a_different_lookup(self):
        tmdb = FakeTmdb(movies={"Hijack": Match("Hijack", "2023")})
        plan(["Hijack.2023.x.mkv", "Hijack.2019.x.mkv"], MOVIES, SHOWS, tmdb)
        self.assertEqual(len(tmdb.calls), 2)

    def test_an_empty_batch_is_not_an_error(self):
        self.assertEqual(plan([], MOVIES, SHOWS, FakeTmdb()), [])


class Transport(unittest.TestCase):
    def test_a_name_carrying_the_separator_is_refused(self):
        decision = Decision("we\x1ford", MOVIE, dest="/x")
        self.assertTrue(unsafe_for_transport(decision, "\x1f"))

    def test_a_destination_carrying_a_newline_is_refused(self):
        decision = Decision("ok.mkv", MOVIE, dest="/x/two\nlines")
        self.assertTrue(unsafe_for_transport(decision, "\x1f"))

    def test_an_ordinary_record_is_fine(self):
        decision = Decision("Show.S01E01.mkv", TV, dest="/x/Show/S01")
        self.assertFalse(unsafe_for_transport(decision, "\x1f"))

    def test_a_tab_is_fine_because_the_separator_is_not_a_tab(self):
        self.assertFalse(unsafe_for_transport(Decision("a\tb.mkv", MOVIE, "/x"), "\x1f"))


class NonUtf8Names(unittest.TestCase):
    """A name put.io produced that is not valid UTF-8 must not take a batch down."""

    def test_a_surrogate_escaped_name_plans_like_any_other(self):
        raw = b"caf\xe9.2001.1080p.mkv".decode("utf-8", "surrogateescape")
        tmdb = FakeTmdb(movies={raw.split(".")[0]: Match("Cafe", "2001")})
        decisions = plan([raw], MOVIES, SHOWS, tmdb)
        self.assertEqual(decisions[0].name, raw)
        self.assertEqual(decisions[0].action, MOVIE)


class CanonicalNames(unittest.TestCase):
    """The rename that stops Plex reading a year out of the release name.

    The failure this pins is not hypothetical.  ``The Good Boy 2025 1080p
    WEB-DL HEVC x265 5 1 BONE.mkv`` was filed — correctly — into
    ``Movies/Good Boy (2026)/`` and kept its release name, so the folder said
    2026 and the file said 2025.  Plex believed the file, matched a different
    film of the same name released in 2025, attached the new file to *that*
    item as a second version, and the request for the 2026 film stayed queued
    with the bytes already on the share.
    """

    def test_a_film_is_named_after_the_match_not_the_release(self):
        tmdb = FakeTmdb(movies={"The Good Boy": Match("Good Boy", "2026")})
        decision = plan(
            ["The Good Boy 2025 1080p WEB-DL HEVC x265 5 1 BONE.mkv"], MOVIES, SHOWS, tmdb
        )[0]
        self.assertEqual(decision.dest, MOVIES + "/Good Boy (2026)")
        self.assertEqual(decision.dest_name, "Good Boy (2026).mkv")

    def test_the_directory_and_the_filename_always_agree_on_the_year(self):
        tmdb = FakeTmdb(movies={"The Good Boy": Match("Good Boy", "2026")})
        decision = plan(["The.Good.Boy.2025.1080p.mkv"], MOVIES, SHOWS, tmdb)[0]
        self.assertTrue(decision.dest.endswith("/" + decision.dest_name[: -len(".mkv")]))

    def test_an_episode_gets_the_conventional_code(self):
        tmdb = FakeTmdb(shows={"show": Match("Show", "2019")})
        decision = plan(["show.s08e02.1080p.web.mkv"], MOVIES, SHOWS, tmdb)[0]
        self.assertEqual(decision.dest, SHOWS + "/Show/S08")
        self.assertEqual(decision.dest_name, "Show - S08E02.mkv")

    def test_the_extension_is_carried_over_untouched(self):
        tmdb = FakeTmdb(movies={"Some Film": Match("Some Film", "2020")})
        decision = plan(["Some.Film.2020.1080p.mp4"], MOVIES, SHOWS, tmdb)[0]
        self.assertEqual(decision.dest_name, "Some Film (2020).mp4")

    def test_a_name_with_no_extension_gets_none_invented(self):
        self.assertEqual(canonical_name("Some Film", "Some Film", "2020"), "Some Film (2020)")

    def test_an_unmatched_name_offers_no_canonical_name(self):
        decisions = plan(["no-year-here.mkv"], MOVIES, SHOWS, FakeTmdb())
        self.assertEqual(decisions[0].action, UNPARSED)
        self.assertEqual(decisions[0].dest_name, "")

    def test_the_empty_year_quirk_is_kept_on_both_sides(self):
        # A film with no release_date is filed by bash as "Title ()", brackets
        # and all.  The filename matches the directory rather than quietly
        # tidying one of them.
        tmdb = FakeTmdb(movies={"Some Film": Match("Some Film", "")})
        decision = plan(["Some.Film.2020.mkv"], MOVIES, SHOWS, tmdb)[0]
        self.assertEqual(decision.dest, MOVIES + "/Some Film ()")
        self.assertEqual(decision.dest_name, "Some Film ().mkv")

    def test_episode_code_does_not_read_a_leading_zero_as_octal(self):
        # The same trap season_directory exists for.
        self.assertEqual(episode_code("08", "09"), "S08E09")

    def test_a_canonical_name_that_could_not_be_carried_is_caught(self):
        decision = Decision("a.mkv", MOVIE, dest="/x/Film (2020)", dest_name="Film\x1f.mkv")
        self.assertTrue(unsafe_for_transport(decision, "\x1f"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
