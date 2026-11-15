"""Where each downloaded file goes — decided in one batch, applied by bash.

This is the seam phase 3 turns on, and the shape of it is the whole design
decision, so it is written down here rather than left to be inferred.

**What moves.**  ``identify_and_organize_media`` used to do four things per
file: parse the name, ask TMDb about it, build a destination directory, and
``mkdir``/``mv`` into it.  The first three are decisions and they all depend
only on the *name*, which is known from the put.io listing long before the
bytes arrive.  So they happen here, for a whole batch, in one process.  The
fourth is a filesystem operation whose correct moment is the instant the file
lands, and it stays in bash.

**Why not move the ``mv`` too.**  The alternative shape — download everything,
then organise everything in one Python process — reads better and is wrong.
The put.io delete is gated on the local file having left staging, so deferring
every move to the end of the run defers every delete to the end of the run,
and a run killed halfway (``putioctl stop``, a reboot, the NAS dropping) then
leaves *everything it downloaded* unorganised in staging instead of just the
file that was in flight.  With ``RCLONE_MAX_CONCURRENT=1`` and multi-gigabyte
files a queue runs for hours, so "killed halfway" is an ordinary Tuesday.
Deciding up front and applying in place keeps the timing of both the library
appearance and the put.io delete exactly where they are today.

**Why not one Python process per file.**  Because that is the rule this
project has already paid for once; see ``putio_python`` in the processor.

The cost of deciding up front is that TMDb is asked about names that may never
download.  The caller bounds it by planning only what actually reaches the
organise path — non-junk, video-extension, not already staged — and by the
lookup cache below, which collapses a ten-episode season pack into one search.

Three of bash's answers are reproduced exactly and are worth knowing about:

* a film TMDb returns with no ``release_date`` is filed as ``Title ()``,
  brackets and all;
* TMDb's ``results[0]`` is taken with no scoring — its relevance order is what
  has been filing this library correctly for years;
* the log lines are bash's, word for word, so a run reads the same as it did
  before the port.  They travel *with* the decision rather than being emitted
  when it is made, so they still appear next to the file they are about.

One answer is deliberately different, and it is the point of the phase.  Bash
cannot tell "TMDb has no such film" from "TMDb refused the key" — ``curl -s``
exits 0 on a 401 and ``jq`` reads no title out of the error body either way —
so both send the file to Unmatched.  A revoked ``TMDB_API_KEY`` therefore
empties a whole run's downloads into ``UNMATCHED_DIR`` with nothing in the log
naming the cause, and the put.io copies are deleted behind them because the
files did leave staging.  Here, a genuine "no results" still means Unmatched,
and a *failed lookup* means :data:`DEFER`: leave the file in staging, say why,
and let put.io keep the copy until the next run.  See the phase 3 notes in
CLAUDE.md for the change in ``process_single_file`` this depends on.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .http import HttpError
from .media import parse_media_info, sanitize_filename

__all__ = [
    "DEFER",
    "MOVIE",
    "TV",
    "UNMATCHED",
    "UNPARSED",
    "Decision",
    "plan",
]

#: Move it into ``dest``, then ``touch`` the movies marker.
MOVIE = "movie"
#: Move it into ``dest``, then ``touch`` the TV marker.
TV = "tv"
#: TMDb answered, and had nothing.  ``UNMATCHED_DIR``, as bash does.
UNMATCHED = "unmatched"
#: The name did not parse at all.  Also ``UNMATCHED_DIR``, but bash logs it
#: differently, so the caller needs to be able to tell the two apart.
UNPARSED = "unparsed"
#: The lookup failed rather than came back empty.  Touch nothing.
DEFER = "defer"


class Decision:
    """One file's destination, plus the lines bash should log on the way.

    Every field is a string.  ``dest`` is a full directory path, empty for
    :data:`DEFER`.  ``dest_name`` is the basename the file should be given
    once it is there — see :func:`canonical_name` for why it exists at all —
    and is empty whenever the release name should be kept.  The two
    ``*_note`` fields are pre-formatted messages and are empty when there is
    nothing to say; the matching ``*_level`` is the level bash should log them
    at.
    """

    __slots__ = (
        "name",
        "action",
        "dest",
        "dest_name",
        "parse_level",
        "parse_note",
        "match_level",
        "match_note",
    )

    def __init__(
        self,
        name: str,
        action: str,
        dest: str = "",
        dest_name: str = "",
        parse_level: str = "",
        parse_note: str = "",
        match_level: str = "",
        match_note: str = "",
    ) -> None:
        self.name = name
        self.action = action
        self.dest = dest
        self.dest_name = dest_name
        self.parse_level = parse_level
        self.parse_note = parse_note
        self.match_level = match_level
        self.match_note = match_note

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Decision(name={0!r}, action={1!r}, dest={2!r})".format(
            self.name, self.action, self.dest
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "action": self.action,
            "dest": self.dest,
            "dest_name": self.dest_name,
            "parse_level": self.parse_level,
            "parse_note": self.parse_note,
            "match_level": self.match_level,
            "match_note": self.match_note,
        }


def season_directory(season: str) -> str:
    """``"1"`` -> ``"S01"``, ``"08"`` -> ``"S08"``.

    **DIVERGENCE from bash, and it is a fix.**  Bash builds this with
    ``printf "%02d" "$season"``, and bash's ``printf`` reads a leading zero as
    an octal prefix: ``printf '%02d' 08`` prints ``00`` and writes
    ``printf: 08: invalid number`` to stderr.  So under the bash, **every S08
    and S09 episode is filed into ``Show/S00``** — which is Plex's Specials
    folder, so they do not merely land in the wrong season, they land somewhere
    Plex treats differently.  Seasons 1-7 and 10+ are unaffected, which is why
    it has gone unnoticed.  The bash has been corrected to ``10#$season`` in
    the same change that added this, so the fallback path agrees.

    Anything that is not a number at all cannot come out of
    ``parse_media_info`` — the pattern captures ``[0-9]{1,2}`` — but this is
    reached from a CLI, so it is made total rather than left to raise.
    """
    try:
        return "S{0:02d}".format(int(season, 10))
    except (TypeError, ValueError):
        return "S" + season


def episode_code(season: str, episode: str) -> str:
    """``("3", "9")`` -> ``"S03E09"``.

    Total for the same reason :func:`season_directory` is: it is reachable
    from a CLI, and a value the parser could not have produced should not
    raise here.
    """
    try:
        return "S{0:02d}E{1:02d}".format(int(season, 10), int(episode, 10))
    except (TypeError, ValueError):
        return "S{0}E{1}".format(season, episode)


def canonical_name(source_name: str, title: str, year: str, code: str = "") -> str:
    """The basename Plex wants, built from the *matched* title rather than the
    release name.

    This exists because of a real filing failure, and the shape of it is worth
    writing down.  Until this was added the processor decided a *directory*
    and moved the file into it under whatever name put.io had — so a release
    called ``The Good Boy 2025 1080p WEB-DL HEVC x265 5 1 BONE.mkv`` landed in
    ``Movies/Good Boy (2026)/``, which is a folder and a filename that
    disagree about the year.  **Plex's movie scanner believes the filename**,
    so it matched the 2025 film of the same name, attached the file to that
    item as a second version, and the request that asked for the 2026 one sat
    in "Queued for library" forever with the bytes already on the share.
    Three different films are called "Good Boy" here, which is what made a
    year disagreement enough to pick the wrong one.

    Naming the file after the answer we already have removes the
    contradiction, and is also what Plex's own layout guidance asks for.  The
    extension is carried over untouched — it is the one part of the release
    name that is not a guess.
    """
    extension = os.path.splitext(source_name)[1]
    if code:
        return "{0} - {1}{2}".format(title, code, extension)
    # QUIRK, inherited from the directory it sits in: an empty year really
    # does produce "Title ()".  Keeping the two consistent matters more than
    # tidying one of them.
    return "{0} ({1}){2}".format(title, year, extension)


class _Lookups:
    """TMDb, asked once per distinct query rather than once per file.

    A season pack is ten files with one show name, and bash searches for it ten
    times — ten HTTP round trips and, at the shipped ``RATE_LIMIT_DELAY=1``,
    ten seconds of sleeping.  Caching the *lookup* changes no answer and no log
    line: the note is still built per file, from the cached match, so the run
    reads exactly as it did.  It only removes the repeat requests.

    Errors are cached too.  Without that, a TMDb outage costs the full retry
    ladder (5s -> 10s -> 20s) for every episode of the pack rather than once.
    """

    def __init__(self, tmdb) -> None:
        self._tmdb = tmdb
        self._cache: Dict[Tuple[str, str, str], Tuple[object, Optional[HttpError]]] = {}

    def search(self, kind: str, query: str, year: str):
        key = (kind, query, year)
        if key not in self._cache:
            try:
                if kind == TV:
                    match = self._tmdb.search_tv(query, year)
                else:
                    match = self._tmdb.search_movie(query, year)
            except HttpError as exc:
                self._cache[key] = (None, exc)
            else:
                self._cache[key] = (match, None)
        match, error = self._cache[key]
        if error is not None:
            raise error
        return match


def _parse_note(info) -> str:
    return (
        "[PARSE] type='{0}', title='{1}', year='{2}', season='{3}', episode='{4}'".format(
            info.media_type, info.title, info.year, info.season, info.episode
        )
    )


def plan(
    names: Iterable[str],
    movie_library: str,
    tv_library: str,
    tmdb,
    on_log: Optional[Callable[[str, str], None]] = None,
) -> List[Decision]:
    """Decide where every name in ``names`` belongs.

    ``tmdb`` is anything with ``search_movie(query, year)`` and
    ``search_tv(query, year)`` — :class:`~putio.tmdb.TmdbClient` in production,
    a stub in the tests.  ``on_log`` is for messages that belong to the batch
    rather than to one file (a rate-limit wait, an HTTP retry); per-file
    messages travel on the :class:`Decision`.

    Names are taken as **basenames**, because that is what
    ``identify_and_organize_media`` calls ``parse_media_info`` on and what it
    will look this plan up by.
    """
    lookups = _Lookups(tmdb)
    decisions = []  # type: List[Decision]

    for name in names:
        info = parse_media_info(name)
        if info is None:
            decisions.append(
                Decision(
                    name,
                    UNPARSED,
                    parse_level="WARN",
                    parse_note="[PARSE] Could not parse media info from filename: " + name,
                )
            )
            continue

        parse_level, parse_note = "INFO", _parse_note(info)
        kind = TV if info.media_type == TV else MOVIE

        try:
            match = lookups.search(kind, info.title, info.year)
        except HttpError as exc:
            # Bash's two wordings, kept, with the cause appended — which is the
            # part bash could not produce, having never seen the status code.
            failed = (
                "[TMDB] Failed to query TMDb for TV show {0}"
                if kind == TV
                else "[TMDB] Failed to query TMDb for {0}"
            ).format(name)
            decisions.append(
                Decision(
                    name,
                    DEFER,
                    parse_level=parse_level,
                    parse_note=parse_note,
                    match_level="ERROR",
                    match_note="{0}: {1}".format(failed, exc),
                )
            )
            continue

        if match is None:
            note = (
                "[TMDB] TV lookup failed for {0} (query: {1}, year: {2})"
                if kind == TV
                else "[TMDB] Lookup failed for {0} (query: {1}, year: {2})"
            ).format(name, info.title, info.year)
            decisions.append(
                Decision(
                    name,
                    UNMATCHED,
                    parse_level=parse_level,
                    parse_note=parse_note,
                    match_level="WARN",
                    match_note=note,
                )
            )
            continue

        sanitized = sanitize_filename(match.title)
        if kind == TV:
            dest = "{0}/{1}/{2}".format(tv_library, sanitized, season_directory(info.season))
            dest_name = canonical_name(
                name, sanitized, match.year, code=episode_code(info.season, info.episode)
            )
            found = "[TMDB] Found TV show: '{0}' ({1})".format(match.title, match.year)
        else:
            # QUIRK: an empty year really does produce "Title ()".  See the
            # module docstring, and putio/tmdb.py's Match.
            dest = "{0}/{1} ({2})".format(movie_library, sanitized, match.year)
            dest_name = canonical_name(name, sanitized, match.year)
            found = "[TMDB] Found movie: '{0}' ({1})".format(match.title, match.year)

        decisions.append(
            Decision(
                name,
                kind,
                dest=dest,
                dest_name=dest_name,
                parse_level=parse_level,
                parse_note=parse_note,
                match_level="INFO",
                match_note=found,
            )
        )

    if on_log is not None and not decisions:
        on_log("DEBUG", "[ORGANIZE] Nothing to plan")

    return decisions


def unsafe_for_transport(decision: Decision, separator: str) -> bool:
    """Would this record survive the shell handoff intact?

    The plan is carried to bash as separator-delimited lines held in a shell
    variable, so a name or a destination containing the separator or a newline
    would be read back as the wrong number of fields — and a *silently* wrong
    destination is the one failure mode worth going out of the way to prevent.
    Such a record is dropped instead of emitted, and bash falls back to its own
    implementation for that one file.  A POSIX filename can contain either
    character; put.io has never produced one that does.
    """
    return any(
        ch in value
        for value in (decision.name, decision.dest, decision.dest_name)
        for ch in (separator, "\n", "\r")
    )
