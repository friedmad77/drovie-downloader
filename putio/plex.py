"""Plex: library discovery, scan triggering, and the Drovie webhook.

Three things the bash does that this module does differently, and each is a
correctness fix rather than a tidy-up.

**Discovery parses XML instead of grepping it.**  ``get_plex_library_id`` runs::

    grep -o "key=\\"[0-9]*\\".*type=\\"$library_type\\"" | head -1 \\
        | grep -o "key=\\"[0-9]*\\"" | cut -d'"' -f2

which works only because this Plex emits one ``<Directory>`` per line and puts
``key`` before ``type``.  Fold the same document onto a single line — which is
a legal serialisation, and what a proxy in front of Plex may well hand back —
and the greedy ``.*`` runs from the first ``key=`` to the *last* matching
``type=``, the second ``grep -o`` then emits every key inside that span, and
the function returns a **multi-line** value like ``"1\\n2"``.  That is then
interpolated straight into the refresh URL.  Measured, not theorised: on this
machine's real ``/library/sections`` response reflowed to one line,
``get_plex_library_id show`` returns two ids.  It has never fired here because
``PLEX_MOVIE_LIBRARY_ID`` and ``PLEX_TV_LIBRARY_ID`` are pinned in the config,
which skips discovery entirely.

**A scan that failed is reported as failed.**  ``curl -s`` exits 0 on a 404, so
``scan_plex_library`` logs "scan triggered successfully" whenever the socket
connected.  Plex answers a nonexistent section id with ``404 Not Found`` as an
HTML page; the bash calls that a success.  See :mod:`putio.http`.

**The section type for TV is ``show``, not ``tv``.**  The bash carries this in
the caller (``if [ "$library_type" = "movie" ] … else get_plex_library_id
"show"``), so the mapping is preserved here in :data:`SECTION_TYPES` rather
than left for each caller to remember.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import Any, Dict, List, Optional

from .http import HttpClient, HttpError, build_url

__all__ = ["PlexClient", "Section", "SECTION_TYPES"]

#: The processor's own name for a library -> the ``type`` attribute Plex uses.
#: ``movie`` happens to be the same on both sides; ``tv`` is not.
SECTION_TYPES = {"movie": "movie", "tv": "show"}


class Section:
    """One ``<Directory>`` from ``/library/sections``."""

    __slots__ = ("key", "type", "title")

    def __init__(self, key: str, type_: str, title: str) -> None:
        self.key = key
        self.type = type_
        self.title = title

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Section(key={0!r}, type={1!r}, title={2!r})".format(
            self.key, self.type, self.title
        )

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "type": self.type, "title": self.title}


class PlexClient:
    """Talks to one Plex server.

    ``base_url`` is ``PLEX_URL`` and is used as given — no normalisation beyond
    stripping a trailing slash, because the bash concatenates
    ``"${PLEX_URL}/library/…"`` and a config with a trailing slash already
    produces a double slash there that Plex accepts.  Stripping it is the one
    change, and it makes the logged URL readable.
    """

    def __init__(self, base_url: str, token: str, http: HttpClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.http = http

    # ------------------------------------------------------------------
    def sections(self) -> List[Section]:
        """Every library, in the order Plex lists them.

        Document order matters: the bash takes the first match via ``head -1``,
        so a server with two movie libraries resolves to the same one either
        way.
        """
        url = build_url(
            "{0}/library/sections".format(self.base_url), {"X-Plex-Token": self.token}
        )
        response = self.http.request(url, description="Plex sections API")

        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as exc:
            # A 200 that is not XML.  Reachable: a reverse proxy that returns
            # its own error page with a 200, or a Plex still starting up.
            raise HttpError(
                "Plex sections response is not XML: {0}".format(exc), url
            )

        found = []
        for element in root.iter("Directory"):
            key = element.get("key")
            if not key:
                continue
            found.append(
                Section(key=key, type_=element.get("type") or "", title=element.get("title") or "")
            )
        return found

    def library_id(self, section_type: str) -> Optional[str]:
        """The ``key`` of the first library of ``section_type``, or None.

        ``section_type`` is Plex's own vocabulary — ``movie`` / ``show`` — not
        the processor's; use :data:`SECTION_TYPES` to translate.
        """
        for section in self.sections():
            if section.type == section_type:
                return section.key
        return None

    # ------------------------------------------------------------------
    def refresh(self, section_id: str) -> None:
        """Trigger a scan of one library.  Raises if Plex refused.

        ``data=b""`` rather than ``None`` so urllib sends ``Content-Length: 0``,
        which is what ``curl -X POST`` with no body sends.  Without it urllib
        emits a POST with no length header at all, and a proxy in front of Plex
        may reject that.
        """
        url = build_url(
            "{0}/library/sections/{1}/refresh".format(self.base_url, section_id),
            {"X-Plex-Token": self.token},
        )
        self.http.request(
            url,
            method="POST",
            data=b"",
            description="Plex section {0} scan".format(section_id),
        )

    # ------------------------------------------------------------------
    def trigger_webhook(self, url: str, key: str) -> None:
        """Poke Drovie's availability check.  Raises if it refused.

        Not a Plex call at all — it goes to Drovie, which is on this same
        machine behind cloudflared — but it lives here because the bash fires
        it from inside ``scan_plex_library`` and the ordering (after the scan,
        after a pause) is the part that matters.
        """
        self.http.request(
            url,
            method="POST",
            data=b"",
            headers={"x-webhook-key": key},
            description="Drovie webhook",
        )
