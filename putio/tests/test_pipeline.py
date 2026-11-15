"""The download loop's branches, in a sandbox.

The invariant these exist for is the one the plan calls the linchpin: **put.io
is deleted only after the local file has left staging.**  Phase 3's review
found the "already exists locally" branch violating it, so every branch that
deletes is checked here for what it does when the organise step leaves the file
where it was.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from putio import organize
from putio.config import Config
from putio.pipeline import Batch, Planner, _is_archive_name
from putio.proc import CommandResult


class FakeRclone:
    """A remote that is a dict of names to sizes, and a record of deletes."""

    def __init__(self, sizes=None, listing=None, produce=True):
        self._sizes = dict(sizes or {})
        self._listing = listing
        self.produce = produce
        self.deleted = []
        self.copies = []

    def sizes(self):
        return dict(self._sizes)

    def size_of(self, remote_file):
        return self._sizes.get(remote_file)

    def list_dir(self, subdir):
        if self._listing is None:
            return sorted(self._sizes)
        return list(self._listing)

    def delete(self, remote_path, description):
        self.deleted.append(remote_path)
        return True

    def copy(self, remote_file, destination, log_path, inplace):
        self.copies.append(remote_file)
        if self.produce:
            with open(os.path.join(destination, os.path.basename(remote_file)), "wb") as h:
                h.write(b"x" * max(1, self._sizes.get(remote_file, 1)))
        return CommandResult([], 0, "", "")


class FakeDownloader:
    """Puts the file where the real one would, or refuses to."""

    def __init__(self, local_dir, succeed=True, on_complete=None):
        self.local_dir = local_dir
        self.succeed = succeed
        self.fetched = []
        self.on_complete = on_complete

    def fetch(self, remote_file, local_dir):
        self.fetched.append(remote_file)
        if not self.succeed:
            return False
        with open(os.path.join(local_dir, os.path.basename(remote_file)), "wb") as handle:
            handle.write(b"x" * 4096)
        if self.on_complete:
            self.on_complete(os.path.basename(remote_file))
        return True


class FakeTmdb:
    """Answers, or refuses.  ``None`` is "TMDb has nothing"; raising is
    "the request failed", which is the difference the whole defer rule is
    built on."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.queries = []

    def _search(self, query, year):
        self.queries.append((query, year))
        if self.error is not None:
            raise self.error
        return self.answer

    search_movie = _search
    search_tv = _search


class FakeMount:
    def __init__(self, enabled=False, allow=True):
        self.enabled = enabled
        self.dead = False
        self.allow = allow

    def check(self, file_name):
        return self.allow


class Match:
    def __init__(self, title, year):
        self.title = title
        self.year = year


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="putio-pipeline-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("local", "movies", "tv", "unmatched"):
            os.makedirs(os.path.join(self.root, name))
        self.local = os.path.join(self.root, "local")
        self.lines = []
        self.records = []

    def build(self, rclone=None, tmdb=None, downloader=None, mount=None, **overrides):
        settings = dict(
            local_dir=self.local,
            movie_library=os.path.join(self.root, "movies"),
            tv_library=os.path.join(self.root, "tv"),
            unmatched_dir=os.path.join(self.root, "unmatched"),
            rclone_max_concurrent=1,
            delete_after_download=True,
        )
        settings.update(overrides)
        config = Config(**settings)
        rclone = rclone if rclone is not None else FakeRclone()
        downloader = downloader if downloader is not None else FakeDownloader(self.local)
        planner = Planner(
            tmdb if tmdb is not None else FakeTmdb(),
            movie_library=config.movie_library,
            tv_library=config.tv_library,
            log=self.log,
        )
        batch = Batch(
            config,
            rclone,
            downloader,
            planner,
            mount if mount is not None else FakeMount(),
            _retrier(),
            self.log,
            self.record,
        )
        return batch, rclone, downloader, planner

    def log(self, level, message):
        self.lines.append((level, message))

    def record(self, *fields):
        self.records.append(tuple(fields))

    def messages(self):
        return [message for _level, message in self.lines]

    def touch(self, name, size=4096):
        path = os.path.join(self.local, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path


def _retrier():
    """A real retrier with a single attempt and no sleeping.

    Real, because the archive tests want ``unzip`` to actually run — a stub
    that reported success without opening the archive would make "extracted,
    organised, marked and deleted" pass on an empty directory.
    """
    from putio.proc import Retrier

    return Retrier(max_attempts=1, delay=0, sleep=lambda _seconds: None)


class ArchiveNameTest(unittest.TestCase):
    def test_it_matches_bashs_zip_rar_r_pattern(self):
        for name in ("a.zip", "a.rar", "a.r00", "a.r01", "a.r123"):
            self.assertTrue(_is_archive_name(name), name)
        for name in ("a.mkv", "a.rarx", "a.r", "a.rab", "a.r0x"):
            self.assertFalse(_is_archive_name(name), name)


class JunkTest(PipelineCase):
    def test_a_junk_name_is_deleted_without_being_downloaded(self):
        batch, rclone, downloader, _p = self.build()
        batch.process("Sample.mkv", junk={"Sample.mkv"})
        self.assertEqual(downloader.fetched, [])
        self.assertEqual(rclone.deleted, ["Sample.mkv"])
        self.assertIn("[JUNK] Skipping junk file: Sample.mkv", self.messages())

    def test_a_small_file_is_junk_even_with_a_clean_name(self):
        rclone = FakeRclone({"Film.2020.mkv": 1000, "Big.2020.mkv": 10 ** 9})
        batch, _r, _d, _p = self.build(rclone=rclone)
        junk = batch.classify_junk(["Film.2020.mkv", "Big.2020.mkv"])
        self.assertEqual(junk, {"Film.2020.mkv"})

    def test_a_size_nobody_can_read_is_not_junk(self):
        """Bash's probe fails open, and so does this — deleting a file because
        rclone would not answer is the wrong direction to be wrong in."""
        batch, _r, _d, _p = self.build(rclone=FakeRclone({}))
        self.assertEqual(batch.classify_junk(["Film.2020.mkv"]), set())


class NonVideoTest(PipelineCase):
    def test_it_is_downloaded_deleted_locally_and_deleted_from_putio(self):
        batch, rclone, downloader, _p = self.build()
        batch.process("notes.doc")
        self.assertEqual(downloader.fetched, ["notes.doc"])
        self.assertFalse(os.path.exists(os.path.join(self.local, "notes.doc")))
        self.assertEqual(rclone.deleted, ["notes.doc"])

    def test_a_failed_download_leaves_the_putio_copy(self):
        batch, rclone, _d, _p = self.build(
            downloader=FakeDownloader(self.local, succeed=False)
        )
        batch.process("notes.doc")
        self.assertEqual(rclone.deleted, [])


class VideoTest(PipelineCase):
    def test_a_matched_film_is_filed_and_the_putio_copy_released(self):
        batch, rclone, _d, _p = self.build(tmdb=FakeTmdb(Match("Some Film", "2020")))
        self.assertTrue(batch.process("Some.Film.2020.mkv"))
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, "movies", "Some Film (2020)", "Some Film (2020).mkv")
            )
        )
        self.assertEqual(rclone.deleted, ["Some.Film.2020.mkv"])
        self.assertIn(("ADDED", "movie"), self.records)

    def test_a_second_release_does_not_overwrite_the_first(self):
        # Two different releases of one film canonicalise to the same basename.
        # Renaming must not be allowed to destroy a file the library already
        # serves, so the release name is kept for the newcomer and Plex sees
        # two versions — which is what it wants anyway.
        existing = os.path.join(self.root, "movies", "Some Film (2020)")
        os.makedirs(existing)
        with open(os.path.join(existing, "Some Film (2020).mkv"), "w") as handle:
            handle.write("first")

        batch, _r, _d, _p = self.build(tmdb=FakeTmdb(Match("Some Film", "2020")))
        self.assertTrue(batch.process("Some.Film.2020.mkv"))

        with open(os.path.join(existing, "Some Film (2020).mkv")) as handle:
            self.assertEqual(handle.read(), "first")
        self.assertTrue(os.path.isfile(os.path.join(existing, "Some.Film.2020.mkv")))

    def test_an_episode_lands_in_its_season_directory(self):
        batch, _r, _d, _p = self.build(tmdb=FakeTmdb(Match("Show", "2019")))
        batch.process("Show.S08E02.1080p.mkv")
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, "tv", "Show", "S08", "Show - S08E02.mkv")
            )
        )
        self.assertIn(("ADDED", "tv"), self.records)

    def test_a_lookup_that_came_back_empty_goes_to_unmatched(self):
        batch, rclone, _d, _p = self.build(tmdb=FakeTmdb(None))
        batch.process("Some.Film.2020.mkv")
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "unmatched", "Some.Film.2020.mkv"))
        )
        self.assertEqual(rclone.deleted, ["Some.Film.2020.mkv"])

    def test_a_lookup_that_FAILED_defers_and_keeps_the_putio_copy(self):
        """The behaviour change phase 3 made and this phase inherits: bash
        cannot see an HTTP status, so a revoked TMDB_API_KEY emptied a whole
        run into Unmatched and deleted every put.io copy behind it."""
        from putio.http import HttpStatusError

        error = HttpStatusError(
            401, "Unauthorized", "https://api.themoviedb.org/3/search/movie", b""
        )
        batch, rclone, _d, _p = self.build(tmdb=FakeTmdb(error=error))
        self.assertFalse(batch.process("Some.Film.2020.mkv"))
        self.assertTrue(os.path.isfile(os.path.join(self.local, "Some.Film.2020.mkv")))
        self.assertEqual(rclone.deleted, [])
        self.assertIn(
            "[ORGANIZE] File still in staging after organize, keeping Put.io copy: "
            "Some.Film.2020.mkv",
            self.messages(),
        )

    def test_a_file_already_in_staging_is_organised_before_anything_is_deleted(self):
        """The exact branch phase 3's review found deleting the put.io copy on
        the strength of the local file merely *existing*."""
        self.touch("Some.Film.2020.mkv")
        batch, rclone, downloader, _p = self.build(tmdb=FakeTmdb(Match("Some Film", "2020")))
        self.assertTrue(batch.process("Some.Film.2020.mkv"))
        self.assertEqual(downloader.fetched, [])
        self.assertEqual(rclone.deleted, ["Some.Film.2020.mkv"])

    def test_and_keeps_it_when_that_organise_fails(self):
        from putio.http import HttpTransportError

        self.touch("Some.Film.2020.mkv")
        batch, rclone, _d, _p = self.build(
            tmdb=FakeTmdb(error=HttpTransportError("down", "https://api.themoviedb.org"))
        )
        self.assertFalse(batch.process("Some.Film.2020.mkv"))
        self.assertEqual(rclone.deleted, [])

    def test_delete_after_download_false_keeps_everything(self):
        batch, rclone, _d, _p = self.build(
            tmdb=FakeTmdb(Match("Some Film", "2020")), delete_after_download=False
        )
        batch.process("Some.Film.2020.mkv")
        self.assertEqual(rclone.deleted, [])


class ArchiveTest(PipelineCase):
    def _zip(self, name, inner):
        import zipfile

        path = os.path.join(self.root, name)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(inner, "x" * 64)
        return path

    def test_a_zip_extracts_organises_marks_and_deletes(self):
        source = self._zip("Some.Film.2020.zip", "Some.Film.2020.mkv")

        class Copier(FakeDownloader):
            def fetch(inner_self, remote_file, local_dir):
                shutil.copy(source, os.path.join(local_dir, remote_file))
                return True

        batch, rclone, _d, _p = self.build(
            downloader=Copier(self.local), tmdb=FakeTmdb(Match("Some Film", "2020"))
        )
        self.assertTrue(batch.process("Some.Film.2020.zip"))
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, "movies", "Some Film (2020)", "Some Film (2020).mkv")
            )
        )
        self.assertTrue(
            os.path.exists(os.path.join(self.local, ".extracted_Some.Film.2020"))
        )
        self.assertIn("Some.Film.2020.zip", rclone.deleted)
        # And no scratch directory left behind.
        self.assertFalse([n for n in os.listdir(self.local) if n.startswith("extracted_")])

    def test_an_extracted_file_that_could_not_be_filed_keeps_the_putio_copy(self):
        from putio.http import HttpTransportError

        source = self._zip("Some.Film.2020.zip", "Some.Film.2020.mkv")

        class Copier(FakeDownloader):
            def fetch(inner_self, remote_file, local_dir):
                shutil.copy(source, os.path.join(local_dir, remote_file))
                return True

        batch, rclone, _d, _p = self.build(
            downloader=Copier(self.local),
            tmdb=FakeTmdb(error=HttpTransportError("down", "https://api.themoviedb.org")),
        )
        self.assertFalse(batch.process("Some.Film.2020.zip"))
        self.assertEqual(rclone.deleted, [])
        # No marker either: the next run must re-extract and try again rather
        # than take the already-extracted branch, which deletes from put.io
        # without being able to check staging at all.
        self.assertFalse(
            os.path.exists(os.path.join(self.local, ".extracted_Some.Film.2020"))
        )

    def test_an_already_extracted_archive_skips_straight_to_cleanup(self):
        self.touch("Some.Film.2020.zip")
        self.touch(".extracted_Some.Film.2020", size=1)
        batch, rclone, downloader, _p = self.build()
        self.assertTrue(batch.process("Some.Film.2020.zip"))
        self.assertEqual(downloader.fetched, [])
        self.assertIn("[SKIP] Archive already extracted: Some.Film.2020.zip", self.messages())
        self.assertEqual(rclone.deleted, ["Some.Film.2020.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.local, "Some.Film.2020.zip")))

    def test_a_missing_first_volume_is_reported_as_incomplete(self):
        """The check is defensive rather than hot: ``process`` downloads the
        volume immediately before asking, so in a normal run the only way to
        reach "missing parts" is a ``.r00`` that is not on disk at all."""
        batch, _r, _d, _p = self.build()
        extracted, complete = batch.archive_check("Film.r00")
        self.assertFalse(extracted)
        self.assertFalse(complete)
        self.assertIn("[RAR] Missing essential part: Film.r00", self.messages())

    def test_a_two_volume_set_is_complete(self):
        """Bash demanded .r01 as well, so ``Movie.rar`` + ``Movie.r00`` failed
        completeness on every run forever."""
        self.touch("Film.rar")
        self.touch("Film.r00")
        batch, _r, _d, _p = self.build()
        _extracted, complete = batch.archive_check("Film.r00")
        self.assertTrue(complete)

    def test_a_non_first_part_is_kept_until_the_first_one_arrives(self):
        batch, rclone, downloader, _p = self.build()
        self.assertTrue(batch.process("Film.r03"))
        self.assertEqual(downloader.fetched, ["Film.r03"])
        self.assertIn(
            "[ARCHIVE] Archive part downloaded, waiting for first part to extract: Film.r03",
            self.messages(),
        )
        # A regular deletion, not the multi-part one — which would take the
        # volumes the first part still needs.
        self.assertEqual(rclone.deleted, ["Film.r03"])

    def test_multipart_deletion_compares_names_exactly(self):
        """Bash interpolated the base name into an unescaped ERE, so with a
        base of ``My.Movie.2019`` the unrelated ``MyXMovieX2019.r01`` matched
        and was deleted."""
        self.touch("My.Movie.2019.rar")
        self.touch("My.Movie.2019.r00")
        self.touch(".extracted_My.Movie.2019", size=1)
        rclone = FakeRclone(
            listing=["My.Movie.2019.rar", "My.Movie.2019.r00", "MyXMovieX2019.r01"]
        )
        batch, _r, _d, _p = self.build(rclone=rclone)
        batch.process("My.Movie.2019.rar")
        self.assertEqual(
            rclone.deleted, ["My.Movie.2019.rar", "My.Movie.2019.r00"]
        )


class LocalSweepTest(PipelineCase):
    def test_leftovers_are_organised_and_counted(self):
        self.touch("Some.Film.2020.mkv")
        self.touch("notes.txt")
        batch, _r, _d, _p = self.build(tmdb=FakeTmdb(Match("Some Film", "2020")))
        self.assertEqual(batch.sweep_local(), 1)
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, "movies", "Some Film (2020)", "Some Film (2020).mkv")
            )
        )

    def test_an_empty_directory_says_so_at_debug_and_counts_nothing(self):
        batch, _r, _d, _p = self.build()
        self.assertEqual(batch.sweep_local(), 0)
        self.assertEqual(
            [level for level, message in self.lines if "[LOCAL]" in message],
            ["DEBUG", "DEBUG"],
        )


class PlannerTest(PipelineCase):
    def test_a_season_pack_is_one_lookup(self):
        tmdb = FakeTmdb(Match("Show", "2019"))
        planner = Planner(tmdb, "/movies", "/tv", self.log)
        planner.plan(["Show.S01E0{0}.1080p.mkv".format(n) for n in range(1, 6)])
        self.assertEqual(len(tmdb.queries), 1)

    def test_a_planned_name_is_not_looked_up_twice(self):
        tmdb = FakeTmdb(Match("Some Film", "2020"))
        planner = Planner(tmdb, "/movies", "/tv", self.log)
        planner.plan(["Some.Film.2020.mkv"])
        planner.plan(["Some.Film.2020.mkv"])
        self.assertEqual(len(tmdb.queries), 1)

    def test_a_deferred_name_IS_looked_up_again(self):
        """A deferral means the lookup failed, and TMDb may be back by the time
        the next batch is planned."""
        from putio.http import HttpTransportError

        tmdb = FakeTmdb(error=HttpTransportError("down", "https://api.themoviedb.org"))
        planner = Planner(tmdb, "/movies", "/tv", self.log)
        planner.plan(["Some.Film.2020.mkv"])
        self.assertEqual(planner.get("Some.Film.2020.mkv").action, organize.DEFER)
        tmdb.error = None
        tmdb.answer = Match("Some Film", "2020")
        planner.plan(["Some.Film.2020.mkv"])
        self.assertEqual(planner.get("Some.Film.2020.mkv").action, organize.MOVIE)


class PoolTest(PipelineCase):
    def test_the_queue_is_worked_through_and_bounded(self):
        rclone = FakeRclone({"a.mkv": 10 ** 9, "b.mkv": 10 ** 9})
        batch, _r, downloader, _p = self.build(
            rclone=rclone, tmdb=FakeTmdb(None), rclone_max_concurrent=2
        )
        batch.run(["a.mkv", "b.mkv"])
        self.assertEqual(sorted(downloader.fetched), ["a.mkv", "b.mkv"])

    def test_an_empty_queue_is_a_debug_line_not_an_info_one(self):
        batch, _r, _d, _p = self.build()
        batch.run([])
        self.assertEqual(
            self.lines, [("DEBUG", "[PARALLEL] Nothing queued for download")]
        )

    def test_one_files_exception_does_not_stop_the_queue(self):
        class Exploding(FakeDownloader):
            def fetch(inner_self, remote_file, local_dir):
                if remote_file == "a.mkv":
                    raise RuntimeError("boom")
                return FakeDownloader.fetch(inner_self, remote_file, local_dir)

        rclone = FakeRclone({"a.mkv": 10 ** 9, "b.mkv": 10 ** 9})
        batch, _r, downloader, _p = self.build(
            rclone=rclone, downloader=Exploding(self.local), tmdb=FakeTmdb(None)
        )
        batch.run(["a.mkv", "b.mkv"])
        self.assertIn("b.mkv", downloader.fetched)
        self.assertTrue(
            any(m.startswith("[PROCESS] Unhandled error on a.mkv:") for m in self.messages())
        )

    def test_a_dead_share_stops_dispatch_rather_than_scheduling_the_rest(self):
        mount = FakeMount(enabled=True, allow=False)
        mount.dead = True
        rclone = FakeRclone({"a.mkv": 10 ** 9, "b.mkv": 10 ** 9})
        batch, _r, downloader, _p = self.build(
            rclone=rclone, mount=mount, tmdb=FakeTmdb(None)
        )
        batch.run(["a.mkv", "b.mkv"])
        self.assertEqual(downloader.fetched, [])
        self.assertTrue(
            any(m.startswith("[PARALLEL] Media share unavailable") for m in self.messages())
        )

    def test_junk_and_non_video_names_are_never_planned(self):
        """A TMDb lookup for a file about to be deleted is a wasted request and
        a wasted RATE_LIMIT_DELAY."""
        tmdb = FakeTmdb(Match("X", "2020"))
        rclone = FakeRclone({"Sample.mkv": 10, "notes.txt": 10 ** 9})
        batch, _r, _d, _p = self.build(rclone=rclone, tmdb=tmdb)
        batch.run(["Sample.mkv", "notes.txt"])
        self.assertEqual(tmdb.queries, [])


if __name__ == "__main__":
    unittest.main()


class MoveSemanticsTest(PipelineCase):
    """``mv``, not ``shutil.move`` — the difference is a file that fails to file.

    A file re-downloaded because a previous run kept the put.io copy has to
    overwrite the one already in the library.  ``shutil.move`` raises on an
    existing destination, which would send it to Unmatched instead.
    """

    def test_an_existing_destination_is_replaced_not_refused(self):
        from putio.fsops import move_into

        library = os.path.join(self.root, "movies")
        with open(os.path.join(library, "Film.mkv"), "wb") as handle:
            handle.write(b"old")
        source = self.touch("Film.mkv", size=3)
        with open(source, "wb") as handle:
            handle.write(b"new")
        move_into(source, library)
        with open(os.path.join(library, "Film.mkv"), "rb") as handle:
            self.assertEqual(handle.read(), b"new")
        self.assertFalse(os.path.exists(source))

    def test_the_same_object_filed_twice_replaces_rather_than_duplicates(self):
        """The deferred-then-redownloaded flow, which happens inside one run.

        The local sweep files the leftover under its canonical name; the pool
        then downloads the very same put.io object again and files it a second
        time.  Declining the rename because the canonical name is taken would
        leave that name *and* the release name side by side — two full-size
        copies of one film that nothing sweeps.
        """
        from putio.fsops import move_into

        library = os.path.join(self.root, "movies")
        for _pass in (1, 2):
            source = self.touch("The.Good.Boy.2025.BONE.mkv")
            with open(source, "wb") as handle:
                handle.write(b"same-bytes")
            landed = move_into(source, library, "Good Boy (2026).mkv")
            self.assertEqual(os.path.basename(landed), "Good Boy (2026).mkv")

        self.assertEqual(
            sorted(n for n in os.listdir(library) if n.endswith(".mkv")),
            ["Good Boy (2026).mkv"],
        )

    def test_a_name_claimed_by_another_thread_is_not_overwritten(self):
        # The claim is O_CREAT|O_EXCL rather than exists-then-move, so a second
        # caller that loses the race keeps its own name instead of replacing a
        # file the library is already serving.  A zero-byte file standing at
        # the canonical name is what that race looks like from here.
        from putio.fsops import move_into

        library = os.path.join(self.root, "movies")
        claimed = os.path.join(library, "Good Boy (2026).mkv")
        open(claimed, "wb").close()

        source = self.touch("Good.Boy.2026.2160p.OTHER.mkv")
        with open(source, "wb") as handle:
            handle.write(b"a different encode")

        landed = move_into(source, library, "Good Boy (2026).mkv")
        self.assertEqual(os.path.basename(landed), "Good.Boy.2026.2160p.OTHER.mkv")
        self.assertEqual(os.path.getsize(claimed), 0)

    def test_organising_over_an_existing_copy_still_releases_putio(self):
        batch, rclone, _d, _p = self.build(tmdb=FakeTmdb(Match("Some Film", "2020")))
        dest = os.path.join(self.root, "movies", "Some Film (2020)")
        os.makedirs(dest)
        with open(os.path.join(dest, "Some.Film.2020.mkv"), "wb") as handle:
            handle.write(b"old")
        self.touch("Some.Film.2020.mkv")
        self.assertTrue(batch.process("Some.Film.2020.mkv"))
        self.assertEqual(rclone.deleted, ["Some.Film.2020.mkv"])


class DispatchThrottleTest(PipelineCase):
    """The pool submits at most ``max_workers`` ahead, as bash's loop did.

    Queueing the whole list up front is simpler and quietly breaks the
    stop-dispatch check: ``submit()`` returns in microseconds, so the share
    never has a chance to die *between* two files.
    """

    def test_a_share_that_dies_mid_queue_stops_the_rest(self):
        mount = FakeMount(enabled=True)

        class Dying(FakeDownloader):
            def fetch(inner_self, remote_file, local_dir):
                mount.dead = True
                mount.allow = False
                return FakeDownloader.fetch(inner_self, remote_file, local_dir)

        rclone = FakeRclone({"a.mkv": 10 ** 9, "b.mkv": 10 ** 9, "c.mkv": 10 ** 9})
        batch, _r, downloader, _p = self.build(
            rclone=rclone,
            mount=mount,
            downloader=Dying(self.local),
            tmdb=FakeTmdb(None),
            rclone_max_concurrent=1,
        )
        batch.run(["a.mkv", "b.mkv", "c.mkv"])
        self.assertEqual(downloader.fetched, ["a.mkv"])
        self.assertTrue(
            any(
                "2 file(s) left for a later run" in message
                for message in self.messages()
            ),
            self.messages(),
        )

    def test_junk_deletion_ignores_delete_after_download(self):
        """QUIRK, and bash's — the setting documented as "test run (no
        deletion)" still destroys junk on put.io."""
        batch, rclone, _d, _p = self.build(delete_after_download=False)
        batch.process("Sample.mkv", junk={"Sample.mkv"})
        self.assertEqual(rclone.deleted, ["Sample.mkv"])


class PartialExtractionTest(PipelineCase):
    """A partial extraction keeps the put.io copy.

    The scratch directory is removed whatever happens, so a file that could not
    be moved out of it is gone. Reporting the ones that *did* move would have
    the caller organise them, write the marker and delete the archive — leaving
    nothing to recover the lost file from.
    """

    def test_a_move_that_fails_fails_the_whole_extraction(self):
        import zipfile

        from putio import extract as extract_module

        path = os.path.join(self.root, "Two.Films.2020.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("A.Film.2020.mkv", "x" * 64)
            archive.writestr("B.Film.2020.mkv", "x" * 64)

        real_move = extract_module._move
        calls = {"n": 0}

        def flaky(source, destination):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("no space left on device")
            return real_move(source, destination)

        class Copier(FakeDownloader):
            def fetch(inner_self, remote_file, local_dir):
                shutil.copy(path, os.path.join(local_dir, remote_file))
                return True

        batch, rclone, _d, _p = self.build(
            downloader=Copier(self.local), tmdb=FakeTmdb(Match("Film", "2020"))
        )
        extract_module._move = flaky
        try:
            self.assertFalse(batch.process("Two.Films.2020.zip"))
        finally:
            extract_module._move = real_move

        self.assertEqual(rclone.deleted, [])
        self.assertFalse(
            os.path.exists(os.path.join(self.local, ".extracted_Two.Films.2020"))
        )
        self.assertIn("[EXTRACT] Failed to process archive: Two.Films.2020.zip",
                      self.messages())
