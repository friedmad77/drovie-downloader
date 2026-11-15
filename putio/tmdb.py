"""TMDb search, as ``identify_and_move_movie`` and ``_tv`` do it inline today.

The bash is four lines of ``curl`` and ``jq`` in each of two functions::

    search_url="…/search/movie?api_key=$TMDB_API_KEY&query=$(… jq -sRr @uri)"
    tmdb_response=$(retry_command … curl -s "$search_url")
    movie_title=$(echo "$tmdb_response" | jq -r '.results[0].title // empty')
    movie_year=$(echo "$tmdb_response"  | jq -r '.results[0].release_date // empty' | cut -d- -f1)

and the whole of its error handling is "if the title came back empty, move the
file to Unmatched".  That single branch covers three quite different
situations — TMDb genuinely has no such film, TMDb rejected the API key, TMDb
returned a 502 — and the log line is the same for all of them.

This module keeps the answers byte-identical and separates those cases:

* a match          -> :class:`Match`
* no results       -> ``None``          (bash: Unmatched, and rightly so)
* anything else    -> :class:`~putio.http.HttpError` (bash: also Unmatched,
  but silently, which is the bug this phase exists to remove)

The field choices are transcriptions and should stay that way.  ``results[0]``
is taken with no scoring of its own — TMDb's relevance order is what has been
filing this library correctly, and second-guessing it here would be exactly the
kind of "improvement" that phase 1's parity harness exists to prevent.

**And an incident proved that rule right, which is worth writing down because
the obvious fix is the wrong one.**  A request for the 2026 ``Good Boy``
downloaded a release called ``The Good Boy 2025 …``; the search for
``("The Good Boy", year=2025)`` returned the 2026 film, and that was *correct*
— the film had a 2025 festival run and a 2026 release date.  Three unrelated
films are called "Good Boy" here, one of them released in 2025.  So the
tempting rule, "prefer the result whose year matches the year in the release
name", would have picked the 2025 dog film and filed the download as a
different movie entirely.  TMDb's own ordering got it right and the year in a
release name did not.

What was missing was never a better guess — it was a *record* that the answer
had moved.  So :meth:`TmdbClient._search` still takes ``results[0]``, and now
says so out loud when the year it returns is not the year that was asked for.
The answer does not change; the log gains the one line that turns "why is this
request still queued" into a grep.  See :meth:`TmdbClient._note_if_year_moved`
for why that condition and not the more obvious one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .http import HttpClient, build_url

__all__ = ["Match", "TmdbClient", "MOVIE_SEARCH_URL", "TV_SEARCH_URL"]

MOVIE_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TV_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"


class Match:
    """The two fields the processor uses, and nothing else.

    ``year`` is a string, not an int, because that is what goes into the
    directory name — and because ``""`` is a real answer.  A film with no
    ``release_date`` is filed by bash as ``Title ()``, brackets and all; a
    Python ``None`` here would silently become ``Title (None)``.
    """

    __slots__ = ("title", "year", "tmdb_id")

    def __init__(self, title: str, year: str, tmdb_id: Optional[int] = None) -> None:
        self.title = title
        self.year = year
        self.tmdb_id = tmdb_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Match(title={0!r}, year={1!r}, tmdb_id={2!r})".format(
            self.title, self.year, self.tmdb_id
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Match):
            return NotImplemented
        return (self.title, self.year, self.tmdb_id) == (other.title, other.year, other.tmdb_id)

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "year": self.year, "tmdb_id": self.tmdb_id}


def _year_of(date_text: Any) -> str:
    """``"2017-10-04"`` -> ``"2017"``, mirroring ``cut -d- -f1``.

    ``cut`` on an empty string gives an empty string, and jq's ``// empty``
    turns a JSON ``null`` into one — so both absent and null land on ``""``
    the same way here.  A date that is not a date at all comes back whole,
    which is also what ``cut`` does with it.
    """
    if not isinstance(date_text, str):
        return ""
    return date_text.split("-")[0]


class TmdbClient:
    """Searches, rate-limited the way the processor rate-limits them.

    ``limiter`` is acquired once per search, in the same place the bash calls
    ``rate_limit_tmdb`` — immediately before building the URL.  Which limiter
    depends on the caller: ``download-batch`` is the run's only TMDb caller and
    gets the in-process :class:`~putio.ratelimit.TokenWindow`; the hand-run
    subcommands, which can overlap a cron run, keep the file-backed
    :class:`~putio.ratelimit.WindowLimiter`.  See :mod:`putio.ratelimit`.
    """

    def __init__(
        self,
        api_key: str,
        http: HttpClient,
        limiter: Any,
        on_note: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.api_key = api_key
        self.http = http
        self.limiter = limiter
        # (level, message).  Called from pool threads, so the callers hand it
        # the same already-locked logger everything else on the pool uses.
        self.on_note = on_note

    # ------------------------------------------------------------------
    def search_movie(self, query: str, year: str = "") -> Optional[Match]:
        """``.results[0].title`` and the year out of ``.release_date``."""
        return self._search(
            MOVIE_SEARCH_URL,
            {"api_key": self.api_key, "query": query, "year": year},
            title_field="title",
            date_field="release_date",
            description="TMDb movie search for {0}".format(query),
        )

    def search_tv(self, query: str, year: str = "") -> Optional[Match]:
        """``.results[0].name`` and the year out of ``.first_air_date``.

        The year parameter is named differently for TV — ``first_air_date_year``
        rather than ``year`` — and sending the wrong one is silently ignored by
        TMDb rather than rejected, so it is worth reading twice.
        """
        return self._search(
            TV_SEARCH_URL,
            {"api_key": self.api_key, "query": query, "first_air_date_year": year},
            title_field="name",
            date_field="first_air_date",
            description="TMDb TV search for {0}".format(query),
        )

    # ------------------------------------------------------------------
    def _search(
        self,
        base: str,
        params: Dict[str, str],
        title_field: str,
        date_field: str,
        description: str,
    ) -> Optional[Match]:
        self.limiter.acquire()

        # build_url drops the empty year, which is what the bash's
        # `if [ -n "$year" ]` does, and percent-encodes the query the way
        # `jq -sRr @uri` does.  See build_url for why not urlencode's default.
        url = build_url(base, params)
        payload = self.http.get_json(url, description=description)

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None

        first = results[0]
        if not isinstance(first, dict):
            return None

        title = first.get(title_field)
        if not isinstance(title, str) or not title:
            # jq's `// empty` treats null and absent alike, and the caller's
            # `[[ -z "$movie_title" ]]` then sends the file to Unmatched.  A
            # result with no title is therefore "no match", not an error.
            return None

        tmdb_id = first.get("id")
        match = Match(
            title=title,
            year=_year_of(first.get(date_field)),
            tmdb_id=tmdb_id if isinstance(tmdb_id, int) else None,
        )
        self._note_if_year_moved(match, results, title_field, date_field, params)
        return match

    # ------------------------------------------------------------------
    def _note_if_year_moved(
        self,
        chosen: Match,
        results: List[Any],
        title_field: str,
        date_field: str,
        params: Dict[str, str],
    ) -> None:
        """Say so when the answer's year is not the year that was asked for.

        This changes no answer, and the condition is chosen from evidence
        rather than from taste.  The obvious check — "are there several films
        of this name in the results?" — does not fire on the case that caused
        the incident: ``search/movie?query=The Good Boy&year=2025`` returns
        *one* film called Good Boy (the 2026 one) plus two unrelated titles,
        because the year filter has already removed the rivals.  Recorded as
        ``tmdb_movie_ambiguous.json`` so that stays pinned.

        What *is* visible at that moment is the disagreement itself: a year of
        2025 was asked for and a 2026 film came back.  That is exactly the
        condition under which the directory the file is filed into stops
        matching the year in its release name — which, before the file was
        renamed to match, is what let Plex match the wrong film entirely.  It
        should be rare: a release name and a TMDb release date normally agree,
        and disagree mainly for festival runs and re-releases.

        Same-named rivals are appended when the results do contain them, since
        that is the other half of the picture when they are there.
        """
        if self.on_note is None:
            return

        asked = params.get("year") or params.get("first_air_date_year") or ""
        if not asked or not chosen.year or chosen.year == asked:
            return

        rivals = []
        for other in results[1:]:
            if not isinstance(other, dict):
                continue
            if other.get(title_field) != chosen.title:
                continue
            rivals.append(
                "{0} (tmdb {1})".format(
                    _year_of(other.get(date_field)) or "no date", other.get("id")
                )
            )

        self.on_note(
            "WARN",
            "[TMDB] '{0}' asked for {1} but matched {2} (tmdb {3}){4}".format(
                params.get("query", ""),
                asked,
                chosen.year,
                chosen.tmdb_id,
                "; same title also at {0}".format(", ".join(rivals)) if rivals else "",
            ),
        )
