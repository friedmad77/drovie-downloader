"""Filename logic, ported from the processor one function at a time.

Three functions live here — ``parse_media_info``, ``is_junk_name`` and
``sanitize_filename`` — and every one of them is a *transcription* of its bash
original, not a better version of it.  That is the whole point of doing these
first: they are regex-dense, do no I/O, and decide where 1,900 media files get
filed.  ``UNMATCHED_DIR`` is empty across 1,814 films and 93 shows, which means
the hand-rolled regexes already run at a full match rate on the content this
machine actually receives.  Any divergence from bash is therefore a regression,
never an improvement.  ``tests/parity_harness.sh`` runs both sides over a large
corpus and fails on the first byte of difference.

Several things below look wrong and are deliberate.  Each is marked ``QUIRK``
with what bash does and why it is reproduced rather than fixed.  Do not tidy
them without changing the bash first and re-running the harness.

Two ambient facts the transcription depends on, both established by probing
bash 3.2 on macOS rather than by reading a manual:

**Bracket expressions keep their backslashes.**  ``[[ $s =~ [\\.\\ _-] ]]`` is
not "dot, space, underscore, hyphen".  Bash hands the pattern to ``regcomp``
with the backslashes intact, and POSIX says a backslash inside a bracket
expression is *literal* — so the set is ``\\``, ``.``, space, ``_``, ``-``, and a
path separator-ish backslash really does separate a title from its year.  The
same rule is what makes :data:`_SEP_S` below match a literal ``s``.

**The C locale is the one that runs.**  ``tr '[:upper:]' '[:lower:]'`` and
``sed``'s ``[[:space:]]`` are locale-sensitive: under ``LC_ALL=en_US.UTF-8``
they fold ``É``→``é`` and strip a non-breaking space, and with the locale unset
they do neither.  Cron sets no locale, so cron gets the ASCII-only behaviour —
and cron is the only thing that ever runs the processor.  This module
reproduces the ASCII-only reading.  (A run started by hand from Terminal.app
inherits ``LANG=en_US.UTF-8`` and so does *not* match this module for non-ASCII
names; see the locale section of the parity harness, which measures it.)
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence

__all__ = [
    "MediaInfo",
    "is_junk_name",
    "parse_media_info",
    "sanitize_filename",
]


# ---------------------------------------------------------------------------
# shell primitives
# ---------------------------------------------------------------------------

#: Arguments bash's ``echo`` builtin eats as options instead of printing.  It
#: accepts any combination of -n, -e and -E, so ``echo "-nE"`` prints nothing at
#: all.  This is reachable: ``sanitize_filename`` is called on a title that came
#: back from TMDb, and its first act is ``echo "$filename"``.
#:
#: ``\Z`` rather than ``$``, here and in :data:`_JUNK_EXTENSION`, and it is not
#: a stylistic choice: Python's ``$`` also matches immediately *before* a
#: trailing newline, while the POSIX ``$`` bash compiles matches only at the end
#: of the string.  With ``$`` a TMDb title of ``"-n\n"`` would be read as an
#: echo flag and sanitize to the empty string, where bash returns ``-n``.
_ECHO_OPTION = re.compile(r"^-[neE]+\Z")

#: ``sed``'s ``[[:space:]]`` in the C locale.  Spelled out rather than written
#: ``\s``, which in Python also matches Unicode spaces — the exact difference
#: the locale note in the module docstring is about.
_C_SPACE = " \t\v\f\r"

_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _ascii_lower(text: str) -> str:
    """``tr '[:upper:]' '[:lower:]'`` with no locale set: A-Z and nothing else."""
    return text.translate(_ASCII_LOWER)


def _capture(text: str, stages: Sequence[Callable[[str], str]]) -> str:
    """Model ``$(echo "$text" | sed ... | sed ...)``.

    Three shell behaviours collapse into this one helper, and all three are
    load-bearing somewhere below:

    * ``echo`` prints nothing for an argument that is only ``-n``/``-e``/``-E``
      flags, so the whole pipeline sees an empty input.
    * ``sed`` works a line at a time, so an anchor like ``^`` or ``$`` binds to
      each line rather than to the string.
    * command substitution strips *all* trailing newlines from the result.
    """
    if _ECHO_OPTION.match(text):
        return ""
    lines = text.split("\n")
    for stage in stages:
        lines = [stage(line) for line in lines]
    return "\n".join(lines).rstrip("\n")


# ---------------------------------------------------------------------------
# is_junk_file — the name-based half
# ---------------------------------------------------------------------------

#: Extensions the processor throws away.  Note ``sample`` and ``readme`` appear
#: here as *extensions* and again below as substrings; the bash has them twice
#: too and the second test makes the first redundant.  Kept as-is: a redundant
#: branch costs nothing and removing it is a behaviour change to argue about.
#:
#: ``\Z``, not ``$`` — see :data:`_ECHO_OPTION`.  This is the only anchored
#: pattern applied to the raw path rather than to one line of it, so it is the
#: one where the difference is observable.
_JUNK_EXTENSION = re.compile(
    r"\.(txt|nfo|jpg|jpeg|png|gif|bmp|exe|dmg|pdf|doc|docx|readme|sample)\Z"
)

#: Matched anywhere in the *whole path*, not just the basename, because bash
#: tests ``$FILE`` and not ``$(basename "$FILE")``.  A release folder called
#: ``YTS.MX`` therefore condemns the promo files under it.
#:
#: **Never applied to a video or an archive** — see :func:`is_junk_name`.
_JUNK_SUBSTRING = re.compile(r"(sample|proof|rarbg|yts|eztv|readme)")

#: The payload a release exists to deliver: the video extensions
#: :data:`putio.extract.VIDEO_EXTENSIONS` lists, plus the archive volumes the
#: pipeline opens.  Duplicated rather than imported because this module is pure
#: and ``extract`` reaches the filesystem.
_PAYLOAD_EXTENSION = re.compile(
    r"\.(mkv|mp4|avi|mov|m4v|wmv|flv|webm|ts|m2ts|zip|rar|r[0-9]+)\Z"
)

#: ``sample`` as a word of its own — ``Movie-sample.mkv``, ``x.SAMPLE.mkv``,
#: ``Sample/movie.mkv`` — rather than as letters inside another word.
_SAMPLE_TOKEN = re.compile(r"(?:^|[/ ._\-\[\(])sample(?:[/ ._\-\]\)]|\Z)")


def is_junk_name(path: str) -> bool:
    """The name-based half of bash's ``is_junk_file``.

    Deliberately *not* the whole function.  Bash also probes the file's size on
    put.io and calls anything under 1MB junk; that half is I/O, not naming, and
    this module has no business reaching the network for it.  It lives with the
    rclone client instead — :meth:`putio.pipeline.Batch.classify_junk` since
    phase 4.

    Takes the full remote path, not a basename — see :data:`_JUNK_SUBSTRING`.

    **A deliberate divergence from bash**, and the only answer this module has
    moved since the parity golden was frozen.  Bash ran the substring rule over
    every name, and from 2026-04-07 (when it began lowercasing first, to catch
    ``.MKV``) that meant ``[EZTVx.to]``, ``YTS.MX`` and ``RARBG`` — uploader
    signatures stamped on *the episode itself* — condemned the video, and the
    junk branch deleted it from put.io without downloading it.  Before that
    date the uppercase tags never matched, which is the only reason it took
    seven months to cost anything.  So for a video or an archive the tracker
    names, ``proof`` and ``readme`` no longer count at all, and ``sample``
    counts only as a word of its own.  Every answer this changes is a 1 that
    became a 0: the rule can now only *spare* a file, never condemn a new one,
    and the parity harness checks exactly that.
    """
    lowered = _ascii_lower(path)
    if _JUNK_EXTENSION.search(lowered):
        return True
    if _PAYLOAD_EXTENSION.search(lowered):
        return bool(_SAMPLE_TOKEN.search(lowered))
    return bool(_JUNK_SUBSTRING.search(lowered))


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

_SANITIZE_UNDERSCORE = re.compile(r"[<>|?*]")
_SANITIZE_LEAD = re.compile(r"^[" + _C_SPACE + r"_]+")
_SANITIZE_TRAIL = re.compile(r"[" + _C_SPACE + r"_]+$")


def _sanitize_line(line: str) -> str:
    # The order is bash's, and it matters: the colon becomes an em dash before
    # the ``[<>|?*]`` class runs, so a colon never becomes an underscore.
    line = line.replace(":", "—")
    line = line.replace('"', "")
    line = _SANITIZE_UNDERSCORE.sub("_", line)
    # QUIRK: backslash becomes a *forward slash*, not an underscore — so a
    # sanitized title can still introduce a path separator.  Reproduced because
    # 1,814 directories were created under this rule.
    line = line.replace("\\", "/")
    line = _SANITIZE_LEAD.sub("", line)
    return _SANITIZE_TRAIL.sub("", line)


def sanitize_filename(name: str) -> str:
    """Make a TMDb title safe to use as a directory name.

    Byte-for-byte equivalent to the five-``sed`` pipeline in bash, including
    the em dash for a colon and the trim of leading/trailing whitespace and
    underscores.
    """
    return _capture(name, (_sanitize_line,))


# ---------------------------------------------------------------------------
# parse_media_info
# ---------------------------------------------------------------------------

#: ``[\.\ _-]`` as regcomp sees it: backslash, dot, space, underscore, hyphen.
_SEP = r"[\\. _-]"

#: ``[\.\s_-]`` as regcomp sees it.  QUIRK: ``\s`` is not a whitespace class in
#: POSIX ERE and not an escape inside a bracket expression, so this set is
#: backslash, dot, **the letter s**, underscore, hyphen — and notably *not* a
#: space.  The consequence is that the parenthesised-year pattern below cannot
#: match ``Title (2009) 1080p``, the single most natural way to write it.  In
#: practice nothing is lost: such a name falls through to the bare-year branch,
#: which extracts the same year.  Left exactly as bash has it.
_SEP_S = r"[\\.s_-]"

_YEAR = r"((19|20)[0-9]{2})"

# ``.`` matches a newline here: bash calls regcomp without REG_NEWLINE, so
# DOTALL is the faithful setting.  ``^`` still binds to the start of the whole
# string in both engines.
_TV_SXXEXX = re.compile(r"(.*)" + _SEP + r"[Ss]([0-9]{1,2})[Ee]([0-9]{1,2})" + _SEP, re.DOTALL)
_TV_NXN = re.compile(r"(.*)" + _SEP + r"([0-9]{1,2})x([0-9]{1,2})" + _SEP, re.DOTALL)
_MOVIE_BARE_YEAR = re.compile(r"(.*)" + _SEP + _YEAR + _SEP, re.DOTALL)
_MOVIE_PAREN_YEAR = re.compile(r"(.*)" + _SEP_S + r"\(" + _YEAR + r"\)" + _SEP_S, re.DOTALL)
_YEAR_BETWEEN_SEPS = re.compile(_SEP + _YEAR + _SEP)
_YEAR_ANYWHERE = re.compile(_YEAR)

_COLLAPSE_SPACES = re.compile(r" +")
_LEADING_SPACES = re.compile(r"^ *")
_TRAILING_SPACES = re.compile(r" *$")


class MediaInfo(object):
    """What bash exports as ``PARSED_TYPE`` and friends.

    Every field is a string, empty rather than ``None`` when bash would leave
    the variable empty — the caller compares them as strings and a ``None``
    leaking into the handoff would print as ``None``.
    """

    __slots__ = ("media_type", "title", "year", "season", "episode")

    def __init__(self, media_type, title, year="", season="", episode=""):
        # type: (str, str, str, str, str) -> None
        self.media_type = media_type
        self.title = title
        self.year = year
        self.season = season
        self.episode = episode

    def as_dict(self) -> Dict[str, str]:
        return {
            "type": self.media_type,
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MediaInfo):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return "MediaInfo({0!r})".format(self.as_dict())


def _title_before_year_glob(filename: str, year: str) -> str:
    """Model bash's ``${filename%% *$year*}``.

    ``%%`` strips the *longest* matching suffix, which is the same as keeping
    the *shortest* prefix — so this is the earliest space that still has the
    year somewhere after it, and everything from that space on is dropped.  Two
    consequences worth knowing, both observed in the real library:

    * ``Blade Runner 2049`` yields the title ``Blade``, because the first space
      already has ``2049`` after it.
    * ``2012 2009 BluRay`` yields the title unchanged, because the matched year
      is the leading ``2012`` and no space precedes it.
    """
    start = 0
    while True:
        space = filename.find(" ", start)
        if space == -1:
            return filename
        if year in filename[space + 1:]:
            return filename[:space]
        start = space + 1


def _clean_title(title: str, year: str) -> str:
    title = title.replace(".", " ").replace("_", " ").replace("-", " ")
    title = _capture(
        title,
        (
            lambda line: _COLLAPSE_SPACES.sub(" ", line),
            lambda line: _TRAILING_SPACES.sub("", _LEADING_SPACES.sub("", line, count=1), count=1),
        ),
    )
    if year:
        # ``sed "s/ *$year *$//"`` — leftmost match only, no /g.  Strips a
        # trailing repeat of the year that survived the separator cleanup, e.g.
        # "Hijack 2023" -> "Hijack".
        trailing_year = re.compile(r" *" + re.escape(year) + r" *$")
        title = _capture(title, (lambda line: trailing_year.sub("", line, count=1),))
        title = _capture(title, (lambda line: _TRAILING_SPACES.sub("", line, count=1),))
    return title


def parse_media_info(filename: str) -> Optional[MediaInfo]:
    """Classify a release name.  ``None`` is bash's ``return 1``.

    The branch order is bash's and cannot be reordered: TV before movie, and
    within movies, a year between separators before a parenthesised year before
    a year anywhere at all.  A name that reaches the last branch gets a title
    derived by :func:`_title_before_year_glob`, which is where the surprising
    results live.
    """
    match = _TV_SXXEXX.match(filename)
    if match:
        info = MediaInfo("tv", match.group(1), season=match.group(2), episode=match.group(3))
    else:
        match = _TV_NXN.match(filename)
        if match:
            info = MediaInfo("tv", match.group(1), season=match.group(2), episode=match.group(3))
        else:
            info = None

    if info is not None:
        # Bash re-runs a *separate* search for the year here, over the whole
        # filename rather than the part left of the episode marker.  So a year
        # that appears only in the release tail still lands on a TV record.
        year_match = _YEAR_BETWEEN_SEPS.search(filename)
        if year_match:
            info.year = year_match.group(1)
    else:
        match = _MOVIE_BARE_YEAR.match(filename)
        if match:
            info = MediaInfo("movie", match.group(1), year=match.group(2))
        else:
            match = _MOVIE_PAREN_YEAR.match(filename)
            if match:
                info = MediaInfo("movie", match.group(1), year=match.group(2))
            else:
                match = _YEAR_ANYWHERE.search(filename)
                if not match:
                    return None
                year = match.group(1)
                info = MediaInfo("movie", _title_before_year_glob(filename, year), year=year)

    info.title = _clean_title(info.title, info.year)
    return info


# ---------------------------------------------------------------------------
# batch entry points
# ---------------------------------------------------------------------------
#
# Everything bash calls is a whole-list-in, whole-list-out function.  An
# interpreter start is ~30ms and is_junk_name runs once per file in the put.io
# listing; one process per item is the mistake this project has already paid
# for once, in rotate_log.


def junk_names(paths: Sequence[str]) -> List[str]:
    """The subset of ``paths`` that are junk by name, in input order."""
    return [path for path in paths if is_junk_name(path)]


def classify(paths: Sequence[str]) -> List[Dict[str, object]]:
    """Every answer this module has about each path, in input order."""
    records = []  # type: List[Dict[str, object]]
    for path in paths:
        info = parse_media_info(path)
        record = {
            "name": path,
            "junk": is_junk_name(path),
            "sanitized": sanitize_filename(path),
            "parsed": info.as_dict() if info is not None else None,
        }  # type: Dict[str, object]
        records.append(record)
    return records
