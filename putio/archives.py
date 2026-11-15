"""Archive rules: marker names, multipart membership, RAR completeness.

The processor's archive handling is seven bash functions that all answer the
same two questions in slightly different words — *what is this archive called
underneath its extension*, and *which other files belong to it* — and each one
spells the answer out again.  ``check_extraction_marker``,
``create_extraction_marker``, ``cleanup_old_markers``, ``cleanup_archive_files``
and ``delete_multipart_rar_from_putio`` each carry their own copy of the
``.rar`` / ``.r00`` / ``.zip`` suffix ladder.  This module has one copy.

**What stays in bash: running ``unzip`` and ``unar``.**  Phase 3 moves the
*rules*, not the extraction.  Shelling out to an archiver from here would buy
nothing — the retry, the logging and the temp directory are all already written
— and would put a subprocess-spawning path into a module whose entire value is
that it is pure and testable.

Everything below is a transcription of the bash, with two exceptions, both
marked ``DIVERGENCE`` where they occur and both reported rather than smuggled:

* :func:`rar_completeness` accepts a two-part ``.rar`` + ``.r00`` set.  Bash
  rejects it and the archive is then never extracted, on any run, forever.
* :func:`putio_part_names` matches part names exactly.  Bash interpolates the
  base name into a regular expression unescaped, so its dots are wildcards —
  which can match, and therefore delete from put.io, a *different* archive.

The rest of the surprises are ``QUIRK``s and are reproduced.  The biggest is
that the suffix ladder is **case-sensitive** while the caller that decides
whether to extract at all lowercases first, so ``Movie.RAR`` is extracted but
gets a marker called ``.extracted_Movie.RAR`` and is treated as a single-part
archive whatever else sits beside it.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    "MARKER_PREFIX",
    "PartCheck",
    "local_cleanup_targets",
    "marker_base",
    "marker_name",
    "orphaned_markers",
    "putio_base",
    "putio_part_names",
    "rar_completeness",
]

#: ``.extracted_<base>`` in ``LOCAL_DIR``.  A dotfile, so it never appears in
#: the ``find`` that picks up unprocessed video files.
MARKER_PREFIX = ".extracted_"

#: The suffix ladder, in bash's order.  Case-sensitive on purpose; see the
#: module docstring.
_MARKER_SUFFIXES = (".rar", ".r00", ".zip")

#: ``"$LOCAL_DIR/${base_name}".r[0-9]*`` as a glob: the letter r, one digit,
#: then anything at all.  Not ``\.r[0-9]+$`` — ``Movie.r0anything`` matches the
#: glob and so counts as a part for the purpose of keeping a marker alive.
_PART_GLOB = re.compile(r"\.r[0-9]")

#: ``^(.*)\.r[0-9]+$``, greedy, as bash compiles it: ``a.r01.r02`` has the base
#: ``a.r01``.
_PART_SUFFIX = re.compile(r"\A(.*)\.r[0-9]+\Z", re.DOTALL)

#: An upper bound bash carries as ``while [ $part_num -lt 1000 ]``.
MAX_PARTS = 1000


# ---------------------------------------------------------------------------
# marker names
# ---------------------------------------------------------------------------


def marker_base(archive_name: str) -> str:
    """The name an extraction marker is keyed on.

    QUIRK — case-sensitive, while ``process_single_file`` decides *whether* to
    extract from a lowercased copy of the same name.  ``Movie.RAR`` therefore
    reaches extraction and is marked as ``.extracted_Movie.RAR``: the marker
    still works (both sides of the comparison are wrong the same way), but the
    archive is treated as single-part by every other function here, so an
    uppercase multipart set has its parts left on put.io one by one.
    """
    for suffix in _MARKER_SUFFIXES:
        if archive_name.endswith(suffix):
            return archive_name[: -len(suffix)]
    return archive_name


def marker_name(archive_name: str) -> str:
    """``Movie.r00`` -> ``.extracted_Movie``."""
    return MARKER_PREFIX + marker_base(archive_name)


def _is_part_of(base: str, name: str) -> bool:
    """Does ``name`` match the ``base.r[0-9]*`` glob bash sweeps with?"""
    if not name.startswith(base):
        return False
    rest = name[len(base) :]
    return bool(_PART_GLOB.match(rest))


def orphaned_markers(entries: Sequence[str]) -> List[str]:
    """Marker files in ``entries`` whose archive is no longer there.

    ``entries`` is one directory listing of ``LOCAL_DIR`` — names, not paths.
    Bash walks the markers and, for each, re-globs the directory looking for
    ``base.rar``, ``base.r00``, ``base.zip`` and then ``base.r[0-9]*``.  Same
    answer, one pass, and no ``basename`` fork per marker.
    """
    present = set(entries)
    orphans = []
    for entry in entries:
        if not entry.startswith(MARKER_PREFIX):
            continue
        base = entry[len(MARKER_PREFIX) :]
        if any(base + suffix in present for suffix in (".rar", ".r00", ".zip")):
            continue
        if any(_is_part_of(base, name) for name in entries):
            continue
        orphans.append(entry)
    return orphans


# ---------------------------------------------------------------------------
# completeness
# ---------------------------------------------------------------------------


class PartCheck:
    """The answer :func:`rar_completeness` gives, and why.

    ``complete`` is what the caller acts on; ``parts`` and ``missing`` exist so
    the log line can say something more useful than "missing parts".
    """

    __slots__ = ("complete", "parts", "missing", "multipart")

    def __init__(
        self,
        complete: bool,
        parts: int = 0,
        missing: Optional[str] = None,
        multipart: bool = False,
    ) -> None:
        self.complete = complete
        self.parts = parts
        self.missing = missing
        self.multipart = multipart

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "PartCheck(complete={0!r}, parts={1!r}, missing={2!r})".format(
            self.complete, self.parts, self.missing
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "complete": self.complete,
            "parts": self.parts,
            "missing": self.missing,
            "multipart": self.multipart,
        }


def rar_completeness(first_part: str, entries: Iterable[str]) -> PartCheck:
    """Are all the volumes of ``first_part``'s archive present?

    ``entries`` is the directory listing.  A ``.zip``, or a ``.rar`` with no
    ``.r00`` beside it, is complete by definition — there is nothing to count.

    **DIVERGENCE from bash, and it is a fix.**  Bash's loop reports a missing
    part as fatal when ``part_num -le 1``, which means it requires both ``.r00``
    *and* ``.r01`` to exist.  A two-volume set — ``Movie.rar`` plus
    ``Movie.r00``, which is what a small release looks like — therefore fails
    completeness on every run forever: the archive is never extracted, never
    marked, and never deleted from put.io, so it is re-checked every five
    minutes for as long as it sits there.  The condition here is ``<= 0``: the
    first volume must exist, and a gap after it ends the count.  ``r00``
    genuinely missing is still fatal, which is the case the bash meant to catch.
    """
    present = set(entries)

    if first_part.endswith(".rar"):
        base = first_part[: -len(".rar")]
        if base + ".r00" not in present:
            return PartCheck(True)
        first_part = base + ".r00"

    if not first_part.endswith(".r00"):
        # Not a multipart archive by these rules — a .zip, or anything else.
        return PartCheck(True)

    base = first_part[: -len(".r00")]
    part_num = 0
    while part_num < MAX_PARTS:
        part = "{0}.r{1:02d}".format(base, part_num)
        if part not in present:
            if part_num <= 0:
                return PartCheck(False, parts=part_num, missing=part, multipart=True)
            break
        part_num += 1

    return PartCheck(True, parts=part_num, multipart=True)


# ---------------------------------------------------------------------------
# what belongs to an archive
# ---------------------------------------------------------------------------


def local_cleanup_targets(archive_name: str, entries: Sequence[str]) -> List[str]:
    """Local files ``cleanup_archive_files`` would remove, in its own order.

    Names, not paths, and only names that are actually present — bash reaches
    the same set through ``rm -f`` on globs that quietly match nothing.
    """
    present = set(entries)

    if archive_name.endswith(".r00"):
        base = archive_name[: -len(".r00")]
        targets = [name for name in entries if _is_part_of(base, name)]
        if base + ".rar" in present:
            targets.append(base + ".rar")
        return targets

    if archive_name.endswith(".rar"):
        base = archive_name[: -len(".rar")]
        if base + ".r00" in present:
            targets = [base + ".rar"] if base + ".rar" in present else []
            targets.extend(name for name in entries if _is_part_of(base, name))
            return targets
        return [archive_name] if archive_name in present else []

    # QUIRK: a .zip, or an uppercase .RAR, takes this branch and only the file
    # itself is removed.  Correct for a zip; for an uppercase multipart set it
    # leaves every other volume in LOCAL_DIR, where cleanup_old_markers then
    # keeps the marker alive on their account.
    return [archive_name] if archive_name in present else []


def putio_base(file_name: str) -> Optional[str]:
    """The archive base name of a put.io entry, or None if it is not a part.

    ``None`` is bash's ``else`` branch, where the caller falls back to deleting
    the single file it was given.
    """
    if file_name.endswith(".rar"):
        return file_name[: -len(".rar")]
    match = _PART_SUFFIX.match(file_name)
    if match:
        return match.group(1)
    return None


def putio_part_names(base: str, listing: Iterable[str]) -> List[str]:
    """Entries in ``listing`` that are volumes of ``base``'s archive.

    **DIVERGENCE from bash, and it is a fix.**  Bash tests
    ``[[ "$putio_file" =~ ^${base_name}\\.(rar|r[0-9]+)$ ]]`` with the base name
    interpolated into the pattern unescaped.  A release name is full of dots,
    and a dot in an ERE matches any character — so with a base of
    ``My.Movie.2019``, the unrelated put.io file ``MyXMovieX2019.r01`` matches
    and is deleted.  Measured, not theorised.  Worse in the other direction: a
    base name containing ``[`` or ``(`` compiles to an invalid pattern, ``=~``
    then returns false for every candidate, and *no* part is ever deleted — the
    archive stays on put.io and is re-downloaded on the next run.

    Comparison here is exact.  ``listing`` order is preserved, because the
    caller logs one line per deletion and the log should read in put.io's order.
    """
    wanted_prefix = base + ".r"
    found = []
    for name in listing:
        if not name.startswith(wanted_prefix):
            continue
        suffix = name[len(wanted_prefix) :]
        if suffix == "ar" or (suffix.isdigit() and suffix.isascii()):
            found.append(name)
    return found
