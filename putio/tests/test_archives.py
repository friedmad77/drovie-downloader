"""Archive rules: the ladder, the parts, and the two things bash got wrong.

Most of what follows pins a transcription.  Two classes pin a *divergence*, and
those are the ones to read first — :class:`TwoVolumeSets` and
:class:`PutioPartMatching`.  Both fix a bash behaviour that is not a quirk to be
preserved but a defect: one makes a legitimate archive un-extractable forever,
the other can delete a different archive from put.io.
"""

from __future__ import annotations

import unittest

from putio.archives import (
    local_cleanup_targets,
    marker_base,
    marker_name,
    orphaned_markers,
    putio_base,
    putio_part_names,
    rar_completeness,
)


class MarkerNames(unittest.TestCase):
    def test_the_suffix_ladder(self):
        self.assertEqual(marker_base("Movie.rar"), "Movie")
        self.assertEqual(marker_base("Movie.r00"), "Movie")
        self.assertEqual(marker_base("Movie.zip"), "Movie")

    def test_a_middle_volume_keeps_its_whole_name(self):
        # Bash's ladder tests .rar/.r00/.zip and nothing else, so .r01 falls
        # through to the `else` and the marker is keyed on the full name.
        # Harmless — extraction only ever starts from .rar or .r00 — but it is
        # why cleanup_old_markers has to glob for parts as well as for bases.
        self.assertEqual(marker_base("Movie.r01"), "Movie.r01")

    def test_the_ladder_is_case_sensitive(self):
        # QUIRK: process_single_file decides *whether* to extract from a
        # lowercased copy of the name, so Movie.RAR is extracted — but every
        # rule here sees the original case and treats it as a single archive.
        self.assertEqual(marker_base("Movie.RAR"), "Movie.RAR")
        self.assertEqual(marker_name("Movie.RAR"), ".extracted_Movie.RAR")

    def test_marker_name_is_a_dotfile(self):
        # It has to be: process_existing_local_files runs `find` for video
        # files in the same directory, and a visible marker would be swept up
        # by the non-video cleanup path.
        self.assertTrue(marker_name("Movie.zip").startswith("."))


class OrphanedMarkers(unittest.TestCase):
    def test_a_marker_with_no_archive_is_orphaned(self):
        self.assertEqual(orphaned_markers([".extracted_Gone"]), [".extracted_Gone"])

    def test_every_base_extension_keeps_a_marker_alive(self):
        for suffix in (".rar", ".r00", ".zip"):
            entries = [".extracted_Movie", "Movie" + suffix]
            self.assertEqual(orphaned_markers(entries), [], suffix)

    def test_a_middle_volume_alone_keeps_the_marker(self):
        # The .r[0-9]* glob is why: a set whose .rar and .r00 have been removed
        # but whose .r07 has not is still "an archive that exists", and
        # removing the marker would have it extracted again.
        self.assertEqual(orphaned_markers([".extracted_Movie", "Movie.r07"]), [])

    def test_the_glob_is_r_digit_anything_not_r_digits(self):
        # `"$base".r[0-9]*` is a glob, not a regex: one digit then anything.
        self.assertEqual(orphaned_markers([".extracted_M", "M.r0junk"]), [])
        self.assertEqual(orphaned_markers([".extracted_M", "M.rx1"]), [".extracted_M"])

    def test_a_prefix_of_another_name_does_not_count(self):
        self.assertEqual(
            orphaned_markers([".extracted_Movie", "MovieTwo.rar"]), [".extracted_Movie"]
        )

    def test_non_markers_are_never_returned(self):
        self.assertEqual(orphaned_markers(["Movie.mkv", "notes.txt"]), [])


class SingleVolume(unittest.TestCase):
    def test_a_lone_rar_is_complete(self):
        self.assertTrue(rar_completeness("Movie.rar", ["Movie.rar"]).complete)

    def test_a_zip_is_complete(self):
        self.assertTrue(rar_completeness("Movie.zip", ["Movie.zip"]).complete)


class TwoVolumeSets(unittest.TestCase):
    """DIVERGENCE — bash rejects a two-volume set, forever.

    ``check_rar_completeness`` counts from ``.r00`` and treats a missing part as
    fatal while ``part_num -le 1``, which means it demands both ``.r00`` *and*
    ``.r01``.  ``Movie.rar`` + ``Movie.r00`` is a legal two-volume set and a
    perfectly ordinary way for a small release to arrive; under the bash it
    fails completeness on every run, so it is never extracted, never marked,
    and never deleted from put.io — it is simply re-checked every five minutes
    for as long as it sits there.  Nothing in the library records this, because
    the failure is that nothing ever arrives.
    """

    def test_rar_plus_r00_is_complete(self):
        check = rar_completeness("Movie.rar", ["Movie.rar", "Movie.r00"])
        self.assertTrue(check.complete)
        self.assertTrue(check.multipart)
        self.assertEqual(check.parts, 1)

    def test_reached_from_the_r00_side_too(self):
        self.assertTrue(rar_completeness("Movie.r00", ["Movie.rar", "Movie.r00"]).complete)

    def test_a_missing_first_volume_is_still_fatal(self):
        check = rar_completeness("Movie.r00", ["Movie.r01", "Movie.r02"])
        self.assertFalse(check.complete)
        self.assertEqual(check.missing, "Movie.r00")


class MultiVolumeSets(unittest.TestCase):
    def test_consecutive_parts_are_counted(self):
        entries = ["Movie.rar"] + ["Movie.r%02d" % n for n in range(5)]
        check = rar_completeness("Movie.rar", entries)
        self.assertTrue(check.complete)
        self.assertEqual(check.parts, 5)

    def test_a_gap_after_the_first_ends_the_count(self):
        # Bash treats a gap above part 1 as "we reached the end", not as a
        # missing part.  It cannot tell the two apart without knowing how many
        # volumes there should be, and nothing on disk says.
        entries = ["Movie.r00", "Movie.r01", "Movie.r05"]
        check = rar_completeness("Movie.r00", entries)
        self.assertTrue(check.complete)
        self.assertEqual(check.parts, 2)

    def test_parts_past_r99_keep_counting(self):
        entries = ["Movie.r%02d" % n for n in range(101)]
        self.assertEqual(rar_completeness("Movie.r00", entries).parts, 101)


class LocalCleanup(unittest.TestCase):
    def test_r00_removes_every_part_and_the_rar(self):
        entries = ["Movie.rar", "Movie.r00", "Movie.r01", "Other.mkv"]
        self.assertEqual(
            sorted(local_cleanup_targets("Movie.r00", entries)),
            ["Movie.r00", "Movie.r01", "Movie.rar"],
        )

    def test_rar_with_parts_removes_them_all(self):
        entries = ["Movie.rar", "Movie.r00", "Movie.r01"]
        self.assertEqual(
            sorted(local_cleanup_targets("Movie.rar", entries)),
            ["Movie.r00", "Movie.r01", "Movie.rar"],
        )

    def test_a_lone_rar_removes_only_itself(self):
        self.assertEqual(local_cleanup_targets("Movie.rar", ["Movie.rar"]), ["Movie.rar"])

    def test_a_zip_removes_only_itself(self):
        self.assertEqual(local_cleanup_targets("Movie.zip", ["Movie.zip"]), ["Movie.zip"])

    def test_nothing_is_returned_for_a_file_that_is_not_there(self):
        self.assertEqual(local_cleanup_targets("Movie.zip", []), [])

    def test_an_unrelated_archive_is_never_swept_up(self):
        entries = ["Movie.r00", "MovieTwo.r00", "MovieTwo.r01"]
        self.assertEqual(local_cleanup_targets("Movie.r00", entries), ["Movie.r00"])


class PutioBase(unittest.TestCase):
    def test_rar_and_numbered_parts(self):
        self.assertEqual(putio_base("Movie.rar"), "Movie")
        self.assertEqual(putio_base("Movie.r01"), "Movie")
        self.assertEqual(putio_base("Movie.r123"), "Movie")

    def test_anything_else_has_no_base(self):
        self.assertIsNone(putio_base("Movie.zip"))
        self.assertIsNone(putio_base("Movie.mkv"))

    def test_the_capture_is_greedy_as_bash_compiles_it(self):
        self.assertEqual(putio_base("a.r01.r02"), "a.r01")


class PutioPartMatching(unittest.TestCase):
    """DIVERGENCE — bash builds this test as an unescaped regex.

    ``[[ "$putio_file" =~ ^${base_name}\\.(rar|r[0-9]+)$ ]]`` interpolates a
    release name straight into a pattern.  Release names are mostly dots, and a
    dot in an ERE matches any character, so the match is far wider than
    intended — and what it matches, it *deletes from put.io*.  A base name
    containing a bracket is worse in the other direction: the pattern does not
    compile, ``=~`` is false for everything, and no part is ever deleted.
    """

    def test_a_dot_is_not_a_wildcard(self):
        listing = ["My.Movie.2019.rar", "MyXMovieX2019.r01", "My.Movie.2019.r01"]
        self.assertEqual(
            putio_part_names("My.Movie.2019", listing),
            ["My.Movie.2019.rar", "My.Movie.2019.r01"],
        )

    def test_a_base_name_with_regex_metacharacters_still_matches_its_own_parts(self):
        listing = ["Movie[2019].rar", "Movie[2019].r00", "Unrelated.mkv"]
        self.assertEqual(
            putio_part_names("Movie[2019]", listing),
            ["Movie[2019].rar", "Movie[2019].r00"],
        )

    def test_listing_order_is_preserved(self):
        listing = ["M.r02", "M.rar", "M.r00"]
        self.assertEqual(putio_part_names("M", listing), listing)

    def test_a_longer_name_that_starts_the_same_is_not_a_part(self):
        self.assertEqual(putio_part_names("M", ["MM.rar", "M.rarx", "M.r0a"]), [])

    def test_non_ascii_digits_are_not_digits(self):
        # str.isdigit() is true for Arabic-Indic numerals; the bracket
        # expression bash compiles is ASCII 0-9 and nothing else.
        self.assertEqual(putio_part_names("M", ["M.r٣"]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
