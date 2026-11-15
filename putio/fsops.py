"""Filesystem operations with the shell's semantics, not Python's defaults.

Small, but not incidental: the two places where ``shutil`` and the coreutils
disagree are both on the organise path, and both disagreements would show up as
a file that failed to be filed rather than as an error anyone would read.
"""

from __future__ import annotations

import errno
import os
import shutil

__all__ = ["move_into", "move", "remove"]


def move(source: str, destination: str) -> None:
    """``mv src dst`` — including replacing a destination that already exists.

    ``shutil.move`` is not ``mv``: given an existing destination it raises,
    where ``mv`` replaces.  The organise path depends on replacement — a file
    re-downloaded because a previous run kept the put.io copy has to overwrite
    the one already in the library, not fail and land in ``UNMATCHED_DIR`` —
    so the rename is done directly and the cross-device case is spelled out
    rather than delegated.

    ``os.replace`` is atomic within a filesystem, which ``mv`` is too.  Across
    filesystems (staging volume to SMB share) neither is, and both copy — and
    ``mv`` carries the mode and times across, so ``copystat`` runs too.  It is
    best-effort because it is not on an SMB share: a mount that refuses to set
    a timestamp must not turn a completed move into a failure, which is exactly
    what ``mv`` does when it warns and carries on.
    """
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copyfile(source, destination)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass
    os.unlink(source)


def _same_size(source: str, target: str) -> bool:
    """Is what is already there the same object we are about to file?

    Size is the whole test, and it is enough because of what the two cases
    actually are: one is the *same put.io object* filed twice in a run, the
    other is a different encode of the same film.  Two distinct releases
    agreeing to the byte is not something to design around.  Unreadable either
    side answers "no", which keeps the file rather than replacing it.
    """
    try:
        return os.path.getsize(source) == os.path.getsize(target)
    except OSError:
        return False


def move_into(source: str, directory: str, name: str = "") -> str:
    """``mv src dir/`` — returns where it landed.

    ``name`` renames on the way in, and what happens when something already
    sits at that name is the whole of this function, because the two reasons
    it can be occupied want opposite answers.

    *The same file, organised twice — replace it.*  A file the previous run
    deferred is filed by the local sweep and then **downloaded again by the
    pool in the same run**, because put.io still holds the copy and the
    listing still names it.  :func:`move` exists to replace in exactly that
    case.  Declining to would leave the canonical name from the first pass and
    the release name from the second sitting side by side: two full-size
    copies of one film that nothing ever sweeps, and two versions of it in
    Plex — which is the confusing state the rename was added to remove.

    *A different release of the same film — keep both.*  A 1080p and a 2160p
    cut both canonicalise to one basename, and replacing would destroy a file
    the library is already serving.  That one keeps its release name, which is
    also what leaves Plex with the two versions it wants.

    :func:`_same_size` is what tells them apart.

    The claim on a free name is made with ``O_CREAT | O_EXCL`` rather than by
    asking :func:`os.path.exists` first.  ``Batch.run`` calls this from a
    :class:`~concurrent.futures.ThreadPoolExecutor` sized by
    ``RCLONE_MAX_CONCURRENT``, so a check-then-move would let two threads
    filing two releases of one film both find the name free and the second
    silently overwrite the first — the loss this guard exists to prevent,
    reintroduced by the shape of the guard.  ``O_EXCL`` makes exactly one of
    them the winner and sends the other down the "keep both" path.
    """
    basename = os.path.basename(source)

    if name:
        target = os.path.join(directory, name)
        try:
            claim = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            # Occupied.  Replace only if it is the same object coming round
            # again; otherwise fall through and keep the release name.
            if _same_size(source, target):
                basename = name
        except OSError:
            # The name cannot be claimed at all (an unwritable directory, say).
            # Keep the release name and let the move report the real failure.
            pass
        else:
            os.close(claim)
            try:
                move(source, target)
            except BaseException:
                # Never leave the empty placeholder behind: a reader would see
                # a zero-byte film, and the next run would take it for the
                # occupied case above.
                remove(target)
                raise
            return target

    destination = os.path.join(directory, basename)
    move(source, destination)
    return destination


def remove(path: str) -> bool:
    """``rm -f``: gone afterwards, and no complaint if it never existed."""
    try:
        os.unlink(path)
        return True
    except OSError:
        return False
