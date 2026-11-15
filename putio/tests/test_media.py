"""Filename logic: the quirks, in writing.

The parity harness is what proves ``putio/media.py`` matches bash — it runs
both over tens of thousands of names.  These tests do the other job: they pin
the handful of results that are *surprising*, so that a later reader who thinks
one of them is a bug has to change a test that says, in words, why it is not.

Anything asserted here was verified against bash 3.2 on macOS with no locale
set, which is the environment cron gives the processor.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from putio.media import (
    classify,
    is_junk_name,
    junk_names,
    parse_media_info,
    sanitize_filename,
)


class ParseTV(unittest.TestCase):
    def parsed(self, name):
        info = parse_media_info(name)
        self.assertIsNotNone(info, name)
        return info.as_dict()

    def test_sxxexx(self):
        self.assertEqual(
            self.parsed("Severance.S02E07.1080p.WEB-DL.x265-NTb.mkv"),
            {"type": "tv", "title": "Severance", "year": "", "season": "02", "episode": "07"},
        )

    def test_lowercase_marker_and_single_digits(self):
        self.assertEqual(
            self.parsed("The.Bear.s3e1.1080p.mkv"),
            {"type": "tv", "title": "The Bear", "year": "", "season": "3", "episode": "1"},
        )

    def test_nxn_form(self):
        self.assertEqual(
            self.parsed("Doctor.Who.12x05.HDTV.mkv"),
            {"type": "tv", "title": "Doctor Who", "year": "", "season": "12", "episode": "05"},
        )

    def test_year_comes_from_anywhere_in_the_name(self):
        # Bash runs a *second*, unanchored search for the year after the
        # episode marker has been captured, over the whole filename — so a year
        # in the release tail lands on the record even though it is not part of
        # the title.
        self.assertEqual(self.parsed("Show.S01E02.2019.1080p.mkv")["year"], "2019")

    def test_multi_episode_is_not_tv(self):
        # QUIRK, and the one worth knowing about: S01E01E02 has no separator
        # after the second episode number, so the TV pattern fails and the name
        # falls through to the movie branches — where it is filed as a film
        # called "Show S01E01E02 1080p".  A double-episode release therefore
        # does not land in the TV library.  It is bash's behaviour, it has
        # never put a file in Unmatched, and changing it is phase 3's call.
        self.assertEqual(
            parse_media_info("Show.S01E01E02.1080p.2019.mkv").as_dict(),
            {"type": "movie", "title": "Show S01E01E02 1080p", "year": "2019",
             "season": "", "episode": ""},
        )

    def test_three_digit_season_is_not_tv(self):
        self.assertEqual(parse_media_info("Show.S001E001.1080p.mkv"), None)


class ParseMovie(unittest.TestCase):
    def parsed(self, name):
        info = parse_media_info(name)
        self.assertIsNotNone(info, name)
        return info.as_dict()

    def test_year_between_separators(self):
        self.assertEqual(
            self.parsed("The.Matrix.1999.2160p.BluRay.x265-FLUX.mkv"),
            {"type": "movie", "title": "The Matrix", "year": "1999", "season": "", "episode": ""},
        )

    def test_backslash_is_a_separator_but_not_a_title_character(self):
        # QUIRK, two halves of it.  The bracket expression [\.\ _-] keeps its
        # backslashes, and POSIX makes a backslash inside brackets literal — so
        # the set really does contain one and a backslash separates a title
        # from its year.  But the title cleanup afterwards only turns ".", "_"
        # and "-" into spaces, so a backslash *inside* the title stays put.
        self.assertEqual(
            self.parsed("The\\Matrix\\1999\\x265.mkv"),
            {"type": "movie", "title": "The\\Matrix", "year": "1999",
             "season": "", "episode": ""},
        )

    def test_parenthesised_year_needs_a_dot_not_a_space(self):
        # QUIRK: the pattern for "(1999)" is written [\.\s_-], and \s inside a
        # bracket expression is a literal backslash and a literal 's', not
        # whitespace.  So the natural spelling does not match that branch — it
        # falls through to the bare-year search, which finds the same year but
        # derives the title by a completely different rule.
        dotted = self.parsed("Tenet.(2020).1080p.mkv")
        spaced = self.parsed("Tenet (2020) 1080p.mkv")
        self.assertEqual(dotted["title"], "Tenet")
        self.assertEqual(spaced["title"], "Tenet")
        self.assertEqual(spaced["year"], "2020")
        # ...and the letter s really is in that set:
        self.assertEqual(self.parsed("Tenets(2020)s1080p.mkv")["title"], "Tenet")

    def test_a_year_in_the_title_is_taken_as_the_release_year(self):
        # A name whose title ends in a year has no way to say which year is
        # which, and greedy matching picks the last one that sits between two
        # separators.  Both spellings therefore keep the whole title and read
        # 2017 as the year, which is the right answer.
        for name in ("Blade Runner 2049 2017 1080p.mkv", "Blade.Runner.2049.2017.1080p.mkv"):
            self.assertEqual(
                self.parsed(name),
                {"type": "movie", "title": "Blade Runner 2049", "year": "2017",
                 "season": "", "episode": ""},
                name,
            )
        # With no release year to find, the title's own year is all there is.
        self.assertEqual(
            self.parsed("Blade Runner 2049.mkv"),
            {"type": "movie", "title": "Blade Runner", "year": "2049",
             "season": "", "episode": ""},
        )

    def test_last_resort_branch_truncates_at_the_first_space(self):
        # QUIRK.  Reached only when no year sits between two separators — here
        # nothing follows 2049 at all.  The branch keeps the shortest prefix
        # that still has the year somewhere after a space, so the title loses
        # everything from its first space on.
        self.assertEqual(
            self.parsed("Blade Runner 2049"),
            {"type": "movie", "title": "Blade", "year": "2049", "season": "", "episode": ""},
        )

    def test_a_numeric_title_is_kept_and_the_release_year_wins(self):
        self.assertEqual(
            self.parsed("2012 2009 BluRay.mkv"),
            {"type": "movie", "title": "2012", "year": "2009", "season": "", "episode": ""},
        )

    def test_leading_year_leaves_the_title_untouched(self):
        # The last-resort branch again: no separator follows either number, so
        # the glob strip looks for a space *before* the matched year 2012,
        # finds none, and the whole string survives as the title.
        self.assertEqual(
            self.parsed("2012 2009BluRay"),
            {"type": "movie", "title": "2012 2009BluRay", "year": "2012",
             "season": "", "episode": ""},
        )

    def test_trailing_year_is_stripped_from_the_title(self):
        self.assertEqual(self.parsed("Hijack 2023.mkv")["title"], "Hijack")

    def test_no_year_at_all_is_a_parse_failure(self):
        self.assertIsNone(parse_media_info("Some.Release.1080p.mkv"))
        self.assertIsNone(parse_media_info(""))


class Junk(unittest.TestCase):
    def test_extensions_are_case_insensitive(self):
        # The incident this whole phase exists to protect: an uppercase .MKV
        # once fell through the video check into the junk path and was deleted
        # locally *and* from put.io.
        self.assertTrue(is_junk_name("notes.NFO"))
        self.assertTrue(is_junk_name("cover.JPG"))
        self.assertFalse(is_junk_name("Film.2001.MKV"))

    def test_substrings_match_the_whole_path(self):
        # bash tests $FILE, not its basename, so a junk word in a directory
        # condemns the promo files under it...
        self.assertTrue(is_junk_name("YTS.MX/YTSProxies.com.url"))
        self.assertTrue(is_junk_name("Some.Release-RARBG/RARBG_DO_NOT_MIRROR.exe"))
        self.assertFalse(is_junk_name("Movies/Film.2001.mkv"))

    def test_a_tracker_tag_never_condemns_the_video(self):
        # ...but not the video. This exact name was deleted from put.io on
        # 2026-11-12 without being downloaded.
        self.assertFalse(is_junk_name(
            "MobLand S02E09 Nervous Breakdown 1080p AMZN WEB-DL DDP5 1 H 264-RAWR[EZTVx.to].mkv"
        ))
        self.assertFalse(is_junk_name("YTS.MX/Film.2001.mkv"))
        self.assertFalse(is_junk_name("Some.Release-RARBG/film.mkv"))
        self.assertFalse(is_junk_name("Film.2001.1080p-YTS.MX.MP4"))
        self.assertFalse(is_junk_name("Death.Proof.2007.1080p.mkv"))
        self.assertFalse(is_junk_name("Release-EZTV/release.part01.rar"))
        self.assertFalse(is_junk_name("Release-EZTV/release.r07"))
        self.assertFalse(is_junk_name("SpongeBob.S09E35.Free.Samples.1080p.mkv"))

    def test_sample_still_condemns_a_video_as_a_word(self):
        self.assertTrue(is_junk_name("Film.2001.1080p-sample.mkv"))
        self.assertTrue(is_junk_name("Film.2001.SAMPLE.mkv"))
        self.assertTrue(is_junk_name("Film.2001/Sample/film.mkv"))
        self.assertTrue(is_junk_name("sample.mkv"))
        self.assertTrue(is_junk_name("Film [sample].mp4"))

    def test_a_tag_still_condemns_a_non_video(self):
        self.assertTrue(is_junk_name("Downloaded from EZTV.url"))
        self.assertTrue(is_junk_name("RARBG.com.mp3"))

    def test_non_ascii_is_not_folded(self):
        # tr '[:upper:]' '[:lower:]' with no locale set touches A-Z only, and
        # cron sets no locale.  "SÄMPLE" is therefore not "sample".
        self.assertFalse(is_junk_name("SÄMPLE.mkv"))
        self.assertTrue(is_junk_name("SAMPLE.mkv"))

    def test_a_trailing_newline_does_not_end_the_name(self):
        # Python's `$` matches before a trailing newline and POSIX's does not,
        # so the extension test is anchored with \Z.  Bash reads "foo.txt\n" as
        # not ending in .txt, and so must this.
        self.assertFalse(is_junk_name("foo.txt\n"))
        self.assertTrue(is_junk_name("foo.txt"))

    def test_batch_preserves_order(self):
        names = ["a.mkv", "b.nfo", "c.mkv", "d.txt"]
        self.assertEqual(junk_names(names), ["b.nfo", "d.txt"])


class Sanitize(unittest.TestCase):
    def test_colon_becomes_an_em_dash(self):
        self.assertEqual(sanitize_filename("Star Wars: Episode IV"), "Star Wars— Episode IV")

    def test_quotes_are_dropped_not_replaced(self):
        self.assertEqual(sanitize_filename('"Wuthering Heights"'), "Wuthering Heights")

    def test_reserved_characters_become_underscores(self):
        self.assertEqual(sanitize_filename("a<b>c|d?e*f"), "a_b_c_d_e_f")

    def test_backslash_becomes_a_forward_slash(self):
        # QUIRK: not an underscore.  A sanitized title can still introduce a
        # path separator, which is how a few nested directories came to exist.
        self.assertEqual(sanitize_filename("back\\slash"), "back/slash")

    def test_whitespace_and_underscores_are_trimmed(self):
        self.assertEqual(sanitize_filename("  _ padded _  "), "padded")
        self.assertEqual(sanitize_filename(""), "")

    def test_non_breaking_space_is_not_whitespace(self):
        # sed's [[:space:]] with no locale set is ASCII.  Under en_US.UTF-8 it
        # would strip this; cron does not run under en_US.UTF-8.
        self.assertEqual(sanitize_filename("title "), "title ")

    def test_echo_swallows_its_own_options(self):
        # QUIRK, and reachable: sanitize_filename's first act is `echo "$1"`,
        # and bash's builtin echo takes -n/-e/-E as flags.
        for option in ("-n", "-e", "-E", "-nE", "-neE"):
            self.assertEqual(sanitize_filename(option), "", option)
        self.assertEqual(sanitize_filename("-x"), "-x")
        self.assertEqual(sanitize_filename("--"), "--")
        # ...but only a word that is *exactly* those flags.  Bash's echo prints
        # "-n\n" verbatim, so the \Z anchor has to hold here too.
        self.assertEqual(sanitize_filename("-n\n"), "-n")


class BatchInterface(unittest.TestCase):
    """The shape bash depends on: one process, whole list in, whole list out."""

    def run_cli(self, args, stdin):
        return subprocess.run(
            [sys.executable, "-B", "-m", "putio"] + args,
            input=stdin,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout

    def test_junk_echoes_back_only_the_junk(self):
        out = self.run_cli(["junk"], b"a.mkv\nb.nfo\nc.mkv\n")
        self.assertEqual(out, b"b.nfo\n")

    def test_null_delimited_round_trip(self):
        out = self.run_cli(["junk", "--null"], b"a\x00b.nfo\x00")
        self.assertEqual(out, b"b.nfo\x00")

    def test_invalid_utf8_survives_the_round_trip(self):
        # put.io has produced names that are not valid UTF-8.  Bash carries
        # them without noticing; surrogateescape is how Python does the same.
        out = self.run_cli(["sanitize", "--null"], b"caf\xe9.2001.mkv\x00")
        self.assertEqual(out, b"caf\xe9.2001.mkv\x00")

    def test_classify_null_record_has_nine_fields(self):
        out = self.run_cli(["classify", "--null"], b"Show.S01E02.x.mkv\x00")
        fields = out.split(b"\x00")
        self.assertEqual(fields[-1], b"")
        self.assertEqual(len(fields) - 1, 9)
        self.assertEqual(fields[3], b"tv")

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(self.run_cli(["classify"], b""), b"")

    def test_blank_lines_are_dropped(self):
        # rclone's listing ends with a newline and bash's own loops skip blank
        # lines, so a blank must not come back as a classified record.
        out = self.run_cli(["classify"], b"\na.mkv\n\n")
        self.assertEqual(len(out.splitlines()), 1)


class Structure(unittest.TestCase):
    def test_classify_answers_for_every_input_in_order(self):
        names = ["Film.2001.mkv", "b.nfo", "nothing"]
        records = classify(names)
        self.assertEqual([r["name"] for r in records], names)
        self.assertEqual([r["junk"] for r in records], [False, True, False])
        self.assertIsNone(records[2]["parsed"])


if __name__ == "__main__":
    unittest.main()
