"""Opening an archive — ``extract_archive``, still shelling to unzip and unar.

``rarfile`` was evaluated and rejected in the plan: it shells to ``unrar`` for
RAR3+ anyway, so it removes nothing and adds a dependency to a package that has
none.  What moves here is the surrounding bookkeeping — the scratch directory,
finding the video files, moving them into ``LOCAL_DIR`` — and one of those was
holding a real defect.

**The scratch directory name was a collision waiting to happen.**  Bash builds
it as ``$LOCAL_DIR/extracted_$(date +%s)``: a *timestamp to the second*, in a
loop that ``download_files_parallel`` runs in up to ``RCLONE_MAX_CONCURRENT``
subshells at once.  Two archives opened in the same second share a directory,
and the first to finish ``rm -rf``s it — taking the other's extracted files
with it, after which that archive reports "No video files found", is never
marked, and is re-extracted on every run.  ``RCLONE_MAX_CONCURRENT`` ships as
1, which is the only reason this has not happened.  :func:`extract` uses
``mkdtemp``, which cannot collide at any concurrency.

The same directory is also a leak: bash removes it on each of its exits, and a
run killed mid-extraction (``putioctl stop``, a reboot) leaves a full-size
directory of video files in ``LOCAL_DIR`` that *nothing* sweeps — not
``staging_sweep``, which only looks at ``STAGING_DIR``, and not
``cleanup_old_markers``.  Here it is a ``finally``.

**Case sensitivity is bash's and is left alone.**  ``process_single_file``
decides *whether* to extract from a lowercased name, and ``extract_archive``
then matches the original — so ``Movie.ZIP`` is sent here and rejected as an
unsupported format.  It is the same mismatch :func:`putio.archives.marker_base`
documents, it has never fired on a real release, and changing it belongs in a
change that can prove which names move.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Callable, List, Optional

from .fsops import move as _move
from .proc import which

__all__ = ["VIDEO_EXTENSIONS", "extract", "is_video_name"]

#: The extensions bash's ``find -iname`` sweep looks for, in its order.
VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
    ".m2ts",
)


def is_video_name(name: str) -> bool:
    """Case-insensitively, is this one of the video extensions?

    Case-insensitive because releases use ``.MKV`` as often as ``.mkv`` — an
    uppercase extension once fell through the video check into the junk-cleanup
    path, which deleted the file locally *and* from put.io.
    """
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in VIDEO_EXTENSIONS)


def extract(
    archive_path: str,
    local_dir: str,
    retrier,
    log: Callable[[str, str], None],
) -> Optional[List[str]]:
    """Open ``archive_path`` and move its video files into ``local_dir``.

    Returns the paths of what was extracted, or ``None`` if the archive could
    not be opened or held nothing playable.  ``None`` and ``[]`` are the same
    answer to the caller — bash tests ``[ $? -eq 0 ] && [ -n "$extracted" ]`` —
    but only ``None`` is returned, so the distinction cannot be relied on by
    mistake.
    """
    archive_name = os.path.basename(archive_path)
    log("INFO", "[EXTRACT] Starting extraction: " + archive_name)

    try:
        extract_dir = tempfile.mkdtemp(prefix="extracted_", dir=local_dir)
    except OSError as exc:
        log("ERROR", "[EXTRACT] Could not create extraction directory: {0}".format(exc))
        return None

    try:
        if archive_name.endswith(".zip"):
            result = retrier.run(
                "unzip " + archive_name,
                ["unzip", "-q", archive_path, "-d", extract_dir],
            )
            if not result.ok:
                return None
        elif archive_name.endswith(".rar") or archive_name.endswith(".r00"):
            if not which("unar"):
                log("ERROR", "[EXTRACT] unar not found. Install with: brew install unar")
                return None
            result = retrier.run(
                "unar " + archive_name,
                [
                    "unar",
                    "-force-overwrite",
                    "-output-directory",
                    extract_dir,
                    archive_path,
                ],
            )
            if not result.ok:
                return None
        else:
            log("ERROR", "[EXTRACT] Unsupported archive format: " + archive_name)
            return None

        found = []  # type: List[str]
        for root, _dirs, names in os.walk(extract_dir):
            for name in names:
                if is_video_name(name):
                    found.append(os.path.join(root, name))
        # Bash takes `find`'s order, which is the directory's.  Sorted here so
        # the log reads the same way twice for the same archive; nothing
        # downstream depends on the order, and a run that is reproducible is
        # easier to compare against another.
        found.sort()

        if not found:
            log("WARN", "[EXTRACT] No video files found in archive: " + archive_name)
            return None

        log(
            "INFO",
            "[EXTRACT] Found {0} video file(s) in {1}".format(len(found), archive_name),
        )

        moved = []  # type: List[str]
        failed = 0
        for source in found:
            video_name = os.path.basename(source)
            destination = os.path.join(local_dir, video_name)
            try:
                _move(source, destination)
            except OSError as exc:
                failed += 1
                log(
                    "ERROR",
                    "[EXTRACT] Failed to move {0} out of the archive: {1}".format(
                        video_name, exc
                    ),
                )
                continue
            moved.append(destination)
            log("INFO", "[EXTRACT] Extracted: " + video_name)

        if failed or not moved:
            # A *partial* extraction is reported as a failure, and that is the
            # invariant rather than pessimism.  The scratch directory is
            # removed in the ``finally`` below whatever happens, so a file that
            # could not be moved out of it is gone — and if this returned the
            # ones that did move, the caller would organise them, write the
            # marker and delete the archive from put.io, leaving nothing to
            # recover the lost file from.  Failing keeps the put.io copy, and
            # the next run re-extracts; anything that did reach LOCAL_DIR is
            # picked up by that run's local sweep.
            #
            # Bash is worse here and silently: its loop echoes every path
            # whether the `mv` worked or not, the caller filters with `-f`, and
            # a file that failed to move simply vanishes from the list — after
            # which the archive is marked and deleted anyway.
            return None
        return moved
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        log("DEBUG", "[EXTRACT] Cleaned up extraction directory for " + archive_name)
