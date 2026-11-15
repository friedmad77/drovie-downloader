"""The download loop and the job pool — ``process_single_file`` and the pool
that dispatched it, in one process.

This is the phase the port's opening rationale was written about.  Bash
hand-rolls the pool because macOS bash 3.2 has no ``wait -n``: it keeps an
array of pids, polls them with ``kill -0`` in a ``sleep 1`` loop, decrements a
counter, and rebuilds the array every pass.  Every job is a background subshell,
so every piece of state a job produces has to leave through the filesystem —
the download count through ``DOWNLOADS_DONE_FILE``, the "share is gone" flag
through ``mount_dead.$$``, the retry stderr through a temp file named after a
pid the subshell had to ask a child for because it has no ``$BASHPID``.

A :class:`~concurrent.futures.ThreadPoolExecutor` removes all of it at once.
The pool is bounded by ``RCLONE_MAX_CONCURRENT`` the same way, jobs are
threads so counters are counters, and — the part that matters beyond tidiness —
**there is no recycled-pid hazard left in the dispatcher**.  ``kill -0`` on a
pid the kernel has since handed to something else reports a finished job as
still running, which is the same class of bug the progress and staging sweeps
needed ``boot_epoch`` to close.

Four things are deliberately unchanged, and three of them are invariants:

* **put.io is deleted only after the local file has left staging.**  Every
  branch that deletes still checks first, and the branch phase 3 found
  violating it — "the file already exists locally", which deleted on the
  strength of the file being *there* — organises first here too.
* **The progress protocol is byte-identical** and published under the *run's*
  pid.  See :mod:`putio.progress`.
* **Decide up front, apply in place.**  The organise decision for a whole
  batch is made before the first byte moves, and the ``mkdir``/move happens the
  instant each file lands.  Deferring every move to the end of the run would
  defer every put.io delete with it, and a run killed halfway — with
  ``RCLONE_MAX_CONCURRENT=1`` and multi-gigabyte files, an ordinary Tuesday —
  would then leave *everything it downloaded* unorganised in staging.
* **Nothing new is persisted.**  A restart still costs nothing, because the
  next run re-derives its work from put.io's listing and the local filesystem.

The one structural addition is :class:`Planner`, which is the shell variable
``PUTIO_ORGANIZE_PLAN`` grown up: a dict instead of a string searched with
``case``, and with the same rule that a ``defer`` verdict is re-planned rather
than reused, because a deferral means the lookup *failed* and TMDb may be back
by the time the next batch is planned.
"""

from __future__ import annotations

import os
import posixpath
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional, Sequence

from . import archives, organize
from .extract import extract, is_video_name
from .fsops import move_into, remove
from .media import is_junk_name

__all__ = ["Batch", "Planner", "ARCHIVE_SUFFIXES"]

#: What ``process_single_file`` treats as an archive part, tested against a
#: lowercased name — ``.zip``, ``.rar`` and ``.r00``-style volumes.
ARCHIVE_SUFFIXES = (".zip", ".rar")

#: Below this, a file on put.io is a decoy rather than a release.
JUNK_SIZE_BYTES = 1048576


def _is_archive_name(lowered: str) -> bool:
    if lowered.endswith(".zip") or lowered.endswith(".rar"):
        return True
    base, _, suffix = lowered.rpartition(".r")
    return bool(base) and suffix.isdigit() and suffix.isascii()


class Planner:
    """Organise decisions for the run, made in batches and looked up by name.

    Batched because a TMDb lookup per file is the shape the batching rule
    forbids, and because the put.io listing supplies every name long before the
    bytes arrive.  There are three natural batches and all three are whole
    lists: the local leftovers, the put.io listing, and the names an archive
    yields the moment it is opened — the only batch the listing could not
    supply.
    """

    def __init__(self, tmdb, movie_library: str, tv_library: str, log) -> None:
        self._tmdb = tmdb
        self._movie_library = movie_library
        self._tv_library = tv_library
        self._log = log
        self._decisions = {}  # type: Dict[str, organize.Decision]
        # Reentrant, and held across the TMDb calls rather than only around the
        # dict.  A second thread arriving mid-batch would otherwise see a name
        # as unplanned and look it up again — the season-pack collapse in
        # organize._Lookups is per call, so the duplicate would be a real extra
        # request and a real extra RATE_LIMIT_DELAY.  Waiting is what it should
        # do: the answer it needs is being fetched.
        self._lock = threading.RLock()

    def plan(self, names: Sequence[str]) -> int:
        """Look up everything in ``names`` that is not already answered.

        A name whose standing verdict is :data:`~putio.organize.DEFER` *is*
        looked up again: a deferral means the lookup failed rather than came
        back empty, and holding a stale one for the rest of the run would send
        a file to the next run for no reason.
        """
        with self._lock:
            wanted = []  # type: List[str]
            seen = set()
            for name in names:
                if not name or name in seen:
                    continue
                standing = self._decisions.get(name)
                if standing is not None and standing.action != organize.DEFER:
                    continue
                seen.add(name)
                wanted.append(name)

            if not wanted:
                return 0

            decisions = organize.plan(
                wanted,
                movie_library=self._movie_library,
                tv_library=self._tv_library,
                tmdb=self._tmdb,
                on_log=self._log,
            )
            for decision in decisions:
                self._decisions[decision.name] = decision
            return len(decisions)

    def get(self, name: str) -> Optional[organize.Decision]:
        with self._lock:
            return self._decisions.get(name)

    def decide(self, name: str) -> Optional[organize.Decision]:
        """The decision for ``name``, planning it now if there is not one.

        The lazy path is what bash reached by falling back per file.  Here it
        is the same code as the batch, just for one name, so a file that
        arrived without being planned gets the same answer rather than a
        second implementation's.
        """
        decision = self.get(name)
        if decision is not None and decision.action != organize.DEFER:
            return decision
        self.plan([name])
        return self.get(name)


class Batch:
    """One run's worth of work: the local sweep, the listing, and the pool.

    :param emit: ``(kind, *fields)`` — the record channel back to bash.
    :param log: ``(level, message)``, replayed by bash through ``log`` so the
        timestamps stay in run order and the format stays the one
        ``rotate_log`` compares as strings.
    """

    def __init__(
        self,
        config,
        rclone,
        downloader,
        planner: Planner,
        mount,
        retrier,
        log: Callable[[str, str], None],
        emit: Callable[..., None],
    ) -> None:
        self._config = config
        self._rclone = rclone
        self._downloader = downloader
        self._planner = planner
        self._mount = mount
        self._retrier = retrier
        self._log = log
        self._emit = emit

    # ------------------------------------------------------------------
    # the local sweep — process_existing_local_files
    # ------------------------------------------------------------------
    def sweep_local(self) -> int:
        """Organise video files left in ``LOCAL_DIR`` by an earlier run.

        These are files a previous run downloaded and could not file — a failed
        move, or a TMDb lookup that deferred — so put.io still holds a copy and
        this is the retry.
        """
        local_dir = self._config.local_dir
        self._log("DEBUG", "[LOCAL] Checking for existing unprocessed video files...")

        try:
            entries = sorted(os.listdir(local_dir))
        except OSError:
            entries = []
        found = [
            os.path.join(local_dir, name)
            for name in entries
            if is_video_name(name) and os.path.isfile(os.path.join(local_dir, name))
        ]

        if not found:
            self._log(
                "DEBUG", "[LOCAL] No unprocessed video files found in " + local_dir
            )
            return 0

        self._log(
            "INFO",
            "[LOCAL] Found {0} unprocessed video file(s) in download directory".format(
                len(found)
            ),
        )
        self._planner.plan([os.path.basename(path) for path in found])

        for path in found:
            self._log("INFO", "[LOCAL] Processing existing file: " + os.path.basename(path))
            self.organize(path)

        self._log("DEBUG", "[LOCAL] Finished processing existing video files")
        return len(found)

    # ------------------------------------------------------------------
    # the pool — download_files_parallel
    # ------------------------------------------------------------------
    def run(self, listing: Sequence[str]) -> None:
        """Plan the listing, then work through it ``max_concurrent`` at a time."""
        queue = [name for name in listing if name]
        if not queue:
            self._log("DEBUG", "[PARALLEL] Nothing queued for download")
            return

        junk = self.classify_junk(queue)
        self._planner.plan(
            [
                posixpath.basename(name)
                for name in queue
                if name not in junk and is_video_name(name)
            ]
        )

        workers = max(1, self._config.rclone_max_concurrent)
        self._log(
            "INFO",
            "[PARALLEL] Queued {0} files for download (max concurrent: {1})".format(
                len(queue), workers
            ),
        )

        dispatched = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            in_flight = set()
            for name in queue:
                # Submit at most `workers` ahead, which is bash's
                # `while active_jobs >= max_jobs` loop without the `kill -0`
                # polling.  Queueing the whole list up front would be simpler
                # and would quietly break the check below: submit() returns in
                # microseconds, so the share would never have a chance to die
                # *between* two files and the run would lose the one line that
                # says how much was left behind.
                while len(in_flight) >= workers:
                    _done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)

                # Stop dispatching once the share is gone.  Without this the
                # whole queue is still scheduled and every job repeats mount
                # recovery on its way to failing.
                if self._mount.enabled and self._mount.dead:
                    self._log(
                        "ERROR",
                        "[PARALLEL] Media share unavailable — stopping dispatch, "
                        "{0} file(s) left for a later run".format(len(queue) - dispatched),
                    )
                    break

                in_flight.add(pool.submit(self._guarded, name, junk))
                dispatched += 1
                self._log(
                    "DEBUG",
                    "[PARALLEL] Started job for: {0}".format(posixpath.basename(name)),
                )
            wait(in_flight)

        self._log("INFO", "[PARALLEL] All downloads completed")

    def _guarded(self, remote_file: str, junk) -> bool:
        """One job, with the same "a failure here is not the run's failure"
        contract the background subshells had — bash never checked their exit
        status at all."""
        try:
            return self.process(remote_file, junk)
        except Exception as exc:  # noqa: BLE001 - one file must not stop the queue
            self._log(
                "ERROR",
                "[PROCESS] Unhandled error on {0}: {1}".format(
                    posixpath.basename(remote_file), exc
                ),
            )
            return False

    # ------------------------------------------------------------------
    # junk
    # ------------------------------------------------------------------
    def classify_junk(self, listing: Sequence[str]):
        """Names that are junk — by name, or by being implausibly small.

        The size half stayed in bash for the whole of phases 1-3 because it is
        an rclone call and :mod:`putio.media` has no business reaching the
        network.  It is still not in :mod:`putio.media`; it is here, where the
        rclone client already is, and it costs no extra call at all now that
        sizes come from one recursive listing rather than one ``lsjson`` per
        file.
        """
        sizes = self._rclone.sizes()
        junk = set()
        for name in listing:
            if is_junk_name(name):
                junk.add(name)
                continue
            size = sizes.get(name.lstrip("/"))
            if size is None:
                size = self._rclone.size_of(name)
            if size is not None and size < JUNK_SIZE_BYTES:
                junk.add(name)
        self._log(
            "DEBUG", "[JUNK] Classified listing: {0} junk".format(len(junk))
        )
        return junk

    # ------------------------------------------------------------------
    # one file — process_single_file
    # ------------------------------------------------------------------
    def process(self, remote_file: str, junk=frozenset()) -> bool:
        config = self._config
        file_name = posixpath.basename(remote_file)
        lowered = file_name.lower()

        if not self._mount.check(file_name):
            return False

        if remote_file in junk:
            # QUIRK, and bash's: this delete does *not* honour
            # DELETE_AFTER_DOWNLOAD.  So the setting documented as "test run
            # (no deletion)" still destroys every junk file on put.io.  Left as
            # it is because changing it is a behaviour change on a path nothing
            # here is otherwise touching.
            self._log("INFO", "[JUNK] Skipping junk file: " + file_name)
            if self._rclone.delete(
                remote_file, "delete junk file " + file_name
            ):
                self._log("INFO", "[PUTIO] Deleted junk file: " + remote_file)
            else:
                self._log("ERROR", "[PUTIO] Failed to delete junk file: " + remote_file)
            return True

        if _is_archive_name(lowered):
            return self._process_archive(remote_file, file_name, lowered)

        local_file = os.path.join(config.local_dir, file_name)

        if not is_video_name(file_name):
            self._log("INFO", "[CLEANUP] Cleaning up non-video file: " + file_name)
            if self._downloader.fetch(remote_file, config.local_dir):
                if os.path.isfile(local_file):
                    remove(local_file)
                    self._log("DEBUG", "[CLEANUP] Deleted local file: " + file_name)
                    self.delete_from_putio(remote_file)
            return True

        # A video file already in LOCAL_DIR is *unorganised* — the local sweep
        # ran earlier in this same run and failed to move it — so deleting the
        # put.io copy on the strength of it existing would leave the only copy
        # in staging with nothing to retry from.  Organise first, delete only
        # if it left.
        if os.path.isfile(local_file):
            self._log("INFO", "[SKIP] File already exists locally: " + file_name)
            self.organize(local_file)
            if os.path.isfile(local_file):
                self._log(
                    "ERROR",
                    "[ORGANIZE] File still in staging after organize, keeping Put.io copy: "
                    + file_name,
                )
                return False
            self.delete_from_putio(remote_file)
            return True

        if not self._downloader.fetch(remote_file, config.local_dir):
            self._log("ERROR", "[DOWNLOAD] Failed to download video file: " + file_name)
            return False

        if not os.path.isfile(local_file):
            self._log("ERROR", "[DOWNLOAD] Video file not found after download: " + local_file)
            return False

        self._log("INFO", "[PROCESS] Processing video file: " + file_name)
        self.organize(local_file)
        if os.path.isfile(local_file):
            self._log(
                "ERROR",
                "[ORGANIZE] File still in staging after organize, keeping Put.io copy: "
                + file_name,
            )
            return False
        self.delete_from_putio(remote_file)
        return True

    # ------------------------------------------------------------------
    def _process_archive(self, remote_file: str, file_name: str, lowered: str) -> bool:
        config = self._config
        self._log("INFO", "[ARCHIVE] Found archive part: " + file_name)
        local_file = os.path.join(config.local_dir, file_name)

        if os.path.isfile(local_file):
            self._log("INFO", "[SKIP] File already exists locally: " + file_name)
        else:
            if not self._downloader.fetch(remote_file, config.local_dir):
                self._log("ERROR", "[DOWNLOAD] Failed to download: " + file_name)
                return False

        if not os.path.isfile(local_file):
            self._log("ERROR", "[ARCHIVE] Archive not found after download: " + local_file)
            return False

        should_extract = lowered.endswith((".rar", ".r00", ".zip"))
        skip_to_cleanup = False

        if should_extract:
            extracted, complete = self.archive_check(file_name)
            if extracted:
                self._log("INFO", "[SKIP] Archive already extracted: " + file_name)
                should_extract = False
                skip_to_cleanup = True
            elif lowered.endswith((".rar", ".r00")) and not complete:
                self._log(
                    "WARN",
                    "[RAR] Skipping extraction of {0} - missing parts".format(file_name),
                )
                return False

        if should_extract:
            self._log("INFO", "[EXTRACT] Attempting extraction of: " + file_name)
            produced = extract(local_file, config.local_dir, self._retrier, self._log)
            if not produced:
                self._log("ERROR", "[EXTRACT] Failed to process archive: " + file_name)
                return False

            self._planner.plan([os.path.basename(path) for path in produced])

            stuck = 0
            for path in produced:
                self._log("INFO", "[PROCESS] Extracted file: " + os.path.basename(path))
                self.organize(path)
                if os.path.isfile(path):
                    stuck += 1

            if stuck:
                # The same rule the video path obeys.  An extracted file can
                # stay put — a failed TMDb lookup defers rather than files —
                # and deleting the archive here would leave the release as an
                # unorganised file in LOCAL_DIR with nothing to retry from.
                # No marker either, so the next run re-extracts and tries
                # again; marking it would send that run down the cleanup
                # branch, which deletes from put.io without checking staging
                # at all.
                self._log(
                    "ERROR",
                    "[ORGANIZE] {0} extracted file(s) still in staging, keeping Put.io copy: {1}".format(
                        stuck, file_name
                    ),
                )
                return False

            self.create_marker(file_name)
            self.archive_cleanup_and_delete(remote_file, file_name)
            return True

        if skip_to_cleanup:
            self.archive_cleanup_and_delete(remote_file, file_name)
            return True

        self._log(
            "INFO",
            "[ARCHIVE] Archive part downloaded, waiting for first part to extract: "
            + file_name,
        )
        # A regular deletion, not the multi-part one: deleting the whole set
        # here would take volumes the first part still needs.
        self.delete_from_putio(remote_file)
        return True

    # ------------------------------------------------------------------
    # archives
    # ------------------------------------------------------------------
    def _local_entries(self) -> List[str]:
        try:
            return os.listdir(self._config.local_dir)
        except OSError:
            return []

    def archive_check(self, archive_name: str):
        """``(already extracted, all volumes present)``."""
        entries = self._local_entries()
        marker = archives.marker_name(archive_name)
        if marker in entries:
            self._log(
                "DEBUG",
                "[MARKER] Found extraction marker: " + archives.marker_base(archive_name),
            )
            # Bash short-circuits here — an archive that is already extracted
            # never reaches the completeness check — so neither does this, or a
            # set whose parts have since been cleaned up would warn about
            # itself.
            return True, True

        check = archives.rar_completeness(archive_name, entries)
        if not check.complete:
            self._log("WARN", "[RAR] Missing essential part: " + (check.missing or ""))
        elif check.multipart:
            self._log(
                "DEBUG",
                "[RAR] Found {0} parts for {1}".format(
                    check.parts, archives.marker_base(archive_name)
                ),
            )
        return False, check.complete

    def create_marker(self, archive_name: str) -> None:
        import time

        path = os.path.join(
            self._config.local_dir, archives.marker_name(archive_name)
        )
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    # `date` with no format, which is what bash writes.
                    # Nothing reads the contents — only the file's existence
                    # matters — but a marker anyone opens should say when.
                    "Extracted on: {0}\n".format(
                        time.strftime("%a %b %e %H:%M:%S %Z %Y")
                    )
                )
        except OSError as exc:
            self._log("WARN", "[MARKER] Could not write extraction marker: {0}".format(exc))
            return
        self._log(
            "DEBUG",
            "[MARKER] Created extraction marker: " + archives.marker_base(archive_name),
        )

    def archive_cleanup_and_delete(self, remote_file: str, file_name: str) -> None:
        """Remove the local volumes, then every part of the set on put.io."""
        config = self._config
        entries = self._local_entries()

        removed = 0
        for target in archives.local_cleanup_targets(file_name, entries):
            if remove(os.path.join(config.local_dir, target)):
                removed += 1
        self._log(
            "DEBUG",
            "[CLEANUP] Deleted {0} local archive file(s) for {1}".format(
                removed, file_name
            ),
        )

        base = archives.putio_base(file_name)
        if not config.delete_after_download:
            self._log(
                "DEBUG",
                "[PUTIO] Skipping deletion (DELETE_AFTER_DOWNLOAD=false): archive "
                + (base or file_name),
            )
            return

        if base is None:
            # Not a RAR part at all — a .zip.  One file, regular deletion.
            self.delete_from_putio(remote_file)
            return

        archive_dir = posixpath.dirname(remote_file)
        archive_dir = "" if archive_dir == "." else (archive_dir + "/" if archive_dir else "")

        listing = self._rclone.list_dir(archive_dir)
        if listing is None:
            self._log(
                "ERROR",
                "[PUTIO] Failed to list files in directory for multi-part deletion: "
                + archive_dir,
            )
            self.delete_from_putio(remote_file)
            return

        self._log("INFO", "[PUTIO] Deleting all parts of multi-part archive: " + base)
        for part in archives.putio_part_names(base, listing):
            full_path = archive_dir + part
            if self._rclone.delete(full_path, "delete archive part " + part):
                self._log("INFO", "[PUTIO] Deleted archive part: " + full_path)
            else:
                self._log("ERROR", "[PUTIO] Failed to delete archive part: " + full_path)

    # ------------------------------------------------------------------
    def delete_from_putio(self, remote_file: str) -> None:
        if not self._config.delete_after_download:
            self._log(
                "DEBUG",
                "[PUTIO] Skipping deletion (DELETE_AFTER_DOWNLOAD=false): " + remote_file,
            )
            return
        if self._rclone.delete(remote_file, "delete from put.io"):
            self._log("INFO", "[PUTIO] Deleted: " + remote_file)
        else:
            self._log("ERROR", "[PUTIO] Failed to delete: " + remote_file)

    # ------------------------------------------------------------------
    # organise — identify_and_organize_media + organize_apply
    # ------------------------------------------------------------------
    def organize(self, file_path: str) -> bool:
        file_name = os.path.basename(file_path)
        decision = self._planner.decide(file_name)

        if decision is None:
            # No answer at all, which the planner only produces for a name it
            # refused to look up.  Bash's own last resort is Unmatched.
            self._log(
                "WARN",
                "[ORGANIZE] Could not parse filename, moving to unmatched: " + file_name,
            )
            return self._to_unmatched(file_path)

        if decision.parse_note:
            self._log(decision.parse_level or "INFO", decision.parse_note)
        if decision.match_note:
            self._log(decision.match_level or "INFO", decision.match_note)

        if decision.action == organize.MOVIE:
            return self.apply(file_path, decision.dest, "movie", decision.dest_name)
        if decision.action == organize.TV:
            return self.apply(file_path, decision.dest, "tv", decision.dest_name)
        if decision.action == organize.UNMATCHED:
            self._log("INFO", "[MOVE] Moving unmatched file to: " + self._config.unmatched_dir)
            return self._to_unmatched(file_path)
        if decision.action == organize.UNPARSED:
            self._log(
                "WARN",
                "[ORGANIZE] Could not parse filename, moving to unmatched: " + file_name,
            )
            return self._to_unmatched(file_path)

        # DEFER: the lookup failed rather than came back empty.  Leave the file
        # exactly where it is — the caller keeps the put.io copy because it
        # never left staging, and the next run tries again.  Bash's own path
        # cannot reach this: it cannot see an HTTP status, so a revoked
        # TMDB_API_KEY empties a whole run into UNMATCHED_DIR irreversibly.
        return False

    def apply(self, file_path: str, dest_dir: str, kind: str, dest_name: str = "") -> bool:
        file_name = os.path.basename(file_path)

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            self._log("ERROR", "[MOVE] Failed to create directory: " + dest_dir)
            self._log("INFO", "[MOVE] Moving unmatched file to: " + self._config.unmatched_dir)
            self._to_unmatched(file_path)
            return False

        try:
            landed = move_into(file_path, dest_dir, dest_name)
        except OSError:
            self._log("ERROR", "[MOVE] Failed to move {0} to {1}/".format(file_name, dest_dir))
            self._log("INFO", "[MOVE] Moving unmatched file to: " + self._config.unmatched_dir)
            self._to_unmatched(file_path)
            return False

        # The rename is named in the log rather than left to be inferred from a
        # later listing: a file whose name on the share differs from the one
        # put.io offered is exactly the thing an operator greps for.
        landed_name = os.path.basename(landed)
        if landed_name != file_name:
            self._log(
                "INFO",
                "[MOVE] Moved {0} to {1}/ as {2}".format(file_name, dest_dir, landed_name),
            )
        else:
            self._log("INFO", "[MOVE] Moved {0} to {1}/".format(file_name, dest_dir))
        # The marker bash `touch`es.  Emitted rather than written so the paths
        # of MOVIES_ADDED_FILE/TV_ADDED_FILE stay in exactly one place — the
        # shell that already owns them and reads them again for the Plex scan.
        self._emit("ADDED", kind)
        return True

    def _to_unmatched(self, file_path: str) -> bool:
        try:
            move_into(file_path, self._config.unmatched_dir)
        except OSError:
            self._log(
                "ERROR",
                "[MOVE] Failed to move {0} to unmatched directory".format(
                    os.path.basename(file_path)
                ),
            )
            return False
        return True
