"""The HTTP layer: ``urllib`` where the processor used ``curl`` piped to ``jq``.

This module exists for one reason that is easy to miss reading the bash it
replaces.  Every API call in the processor is some form of::

    retry_command "TMDb movie search for $q" curl -s "$url"

and ``curl -s`` **exits 0 for 401, 404 and 500 alike**.  It only fails on a
transport error — DNS, connection refused, TLS.  So:

* ``retry_command`` never retried an HTTP error, because it never saw one.  It
  retried transport errors only, three times, 5s → 10s → 20s.
* ``scan_plex_library`` logs ``"$library_type library scan triggered
  successfully"`` whenever the socket connected.  A Plex that answered
  ``404 Not Found`` — which it does, in HTML, for a section id that does not
  exist — is reported as a success.
* A TMDb key that has been revoked returns ``401`` with a JSON body carrying
  ``status_message``.  ``jq -r '.results[0].title // empty'`` reads no title
  from it, so the caller logs "Lookup failed" and moves the file to Unmatched.
  A whole library can go to Unmatched with nothing in the log naming the cause.

:func:`request` raises on a non-2xx status and carries the status and a body
excerpt on the exception, which is the entire "real API error messages" that
this phase of the port is meant to ship.

Two deliberate divergences from the bash, both noted here rather than hidden:

**There is a timeout.**  The ``curl`` calls pass no ``--max-time``, so a Plex
that accepts a connection and then never answers hangs the run forever — while
holding the lockfile, so every subsequent cron tick exits as "already running".
:data:`DEFAULT_TIMEOUT` bounds that.

**Retries are status-aware.**  ``429`` and ``5xx`` are retried with the same
backoff bash used for transport errors; ``4xx`` is not, because retrying a
revoked key three times only delays the error message.  Bash retried none of
these, having no way to see them, so nothing that used to work stops working.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional

__all__ = [
    "DEFAULT_TIMEOUT",
    "HttpError",
    "HttpStatusError",
    "HttpTransportError",
    "HttpClient",
    "Response",
    "redact_url",
]

#: Seconds before a request is abandoned.  Generous, because a Plex scan on a
#: cold library server can take a while to acknowledge, but finite — see the
#: module docstring for what "infinite" costs.
DEFAULT_TIMEOUT = 30.0

#: How much of an error body is worth putting in a log line.  Plex answers
#: errors with a full HTML page; the useful part is the ``<title>``, and the
#: rest would wrap the log for no gain.
BODY_EXCERPT = 300

#: Identifies the caller to TMDb and Plex.  urllib's default is
#: ``Python-urllib/3.x``, which some WAFs in front of a webhook endpoint reject
#: outright — and the failure looks like the endpoint being down.
USER_AGENT = "putio-plex-processor/1 (+https://github.com/putio-plex-processor)"

#: Query parameters whose values must never reach the log.  Matched
#: case-insensitively on the name; the value is replaced wholesale rather than
#: truncated, because a prefix of an API key is still a prefix of an API key.
_SECRET_PARAMS = ("api_key", "x-plex-token", "token", "apikey", "key")

_QUERY_PAIR = re.compile(r"([^&=?]+)=([^&]*)")


def redact_url(url: str) -> str:
    """A URL safe to log.

    Both credentials in this project travel as query parameters —
    ``?api_key=…`` for TMDb and ``?X-Plex-Token=…`` for Plex — so any log line
    naming a URL is a log line leaking a key unless it goes through here.  The
    bash never logged a URL at all, which is why it never had this problem;
    Python logging the URL is what makes the error messages useful, so the
    redaction has to come with it.

    Operates on the raw string rather than parsing and re-assembling, so a URL
    that ``urlsplit`` would normalise comes back recognisably the same.
    """

    def scrub(match: "re.Match[str]") -> str:
        name, value = match.group(1), match.group(2)
        if name.lower().lstrip("?&") in _SECRET_PARAMS and value:
            return "{0}=<redacted>".format(name)
        return match.group(0)

    return _QUERY_PAIR.sub(scrub, url)


class HttpError(Exception):
    """Base for anything that stopped a request from producing a body."""

    def __init__(self, message: str, url: str) -> None:
        super().__init__(message)
        self.url = redact_url(url)


class HttpTransportError(HttpError):
    """No usable HTTP response: DNS, refused connection, TLS, timeout.

    This is the only failure ``curl -s`` reported to the bash, and so the only
    one ``retry_command`` ever acted on.
    """


class HttpStatusError(HttpError):
    """An HTTP response the server refused to make useful — 4xx or 5xx.

    ``status`` and ``excerpt`` are the two things the bash could not see.
    """

    def __init__(self, status: int, reason: str, url: str, body: bytes) -> None:
        self.status = status
        self.reason = reason
        self.excerpt = _excerpt(body)
        message = "HTTP {0} {1} from {2}".format(status, reason, redact_url(url))
        if self.excerpt:
            message += ": " + self.excerpt
        super().__init__(message, url)


def _excerpt(body: bytes) -> str:
    """One line of an error body, worth reading in a log.

    Plex answers with an HTML page whose only informative part is the title, so
    that is lifted out when present.  TMDb answers with JSON carrying
    ``status_message``, which is lifted the same way.  Anything else is
    collapsed to a single line and truncated.

    Every path goes out through :func:`redact_url`, because this is
    *server-controlled text on its way into a log the processor keeps for
    weeks*.  A proxy or WAF that cannot reach TMDb routinely names the request
    it failed on — ``could not connect upstream for
    https://api.themoviedb.org/3/search/movie?api_key=…`` — and redacting the
    URL the request went to (as :class:`HttpStatusError` does) is no use if the
    body quotes it straight back.
    """
    if not body:
        return ""
    text = body.decode("utf-8", "replace").strip()

    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            message = parsed.get("status_message") or parsed.get("message")
            if isinstance(message, str) and message:
                return _safe(message)

    title = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if title:
        collapsed = " ".join(title.group(1).split())
        if collapsed:
            return _safe(collapsed)

    return _safe(" ".join(text.split()))


def _safe(text: str) -> str:
    """Redact, then truncate.

    That order matters: truncating first can cut a URL in half mid-key, and
    ``redact_url`` would then not recognise the fragment as a parameter at all.
    """
    return redact_url(text)[:BODY_EXCERPT]


class Response:
    """What came back, once the status said it was worth having."""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    def json(self) -> Any:
        """Parse the body as JSON, raising :class:`HttpError` if it is not.

        A truncated or HTML body reaching a JSON caller is a real failure mode
        — a captive-portal DNS answer looks exactly like this — and it has to
        arrive as an HttpError rather than a ValueError so the one ``except``
        at the call site catches it.
        """
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(
                "response from {0} is not JSON: {1}".format(redact_url(self.url), exc),
                self.url,
            )

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _is_retryable_status(status: int) -> bool:
    """429 and 5xx are worth another attempt; nothing else is.

    A 401 from a revoked TMDb key and a 404 from a Plex section that was
    deleted are both permanent for the length of this run, and retrying them
    three times with a doubling delay adds 35 seconds to the failure.
    """
    return status == 429 or 500 <= status <= 599


class HttpClient:
    """Requests with the processor's own retry policy.

    ``max_retries`` and ``retry_delay`` come from the same ``MAX_RETRIES`` and
    ``RETRY_DELAY`` the bash ``retry_command`` uses, and the backoff doubles
    the same way (5s → 10s → 20s at the defaults), so a run's worst-case
    duration does not change shape.

    ``sleep`` and ``opener`` are injectable so the tests can drive the retry
    path without sleeping and without a network.  Nothing in the suite opens a
    socket; the fixtures under ``tests/fixtures/`` are recorded responses.
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: int = 5,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
        sleep: Optional[Callable[[float], None]] = None,
        opener: Optional[Callable[..., Any]] = None,
        on_retry: Optional[Callable[[str], None]] = None,
    ) -> None:
        # A configured 0 must not mean "never attempt".  The bash loop is
        # `while [ $attempt -le $max_attempts ]`, so a 0 there would skip the
        # command entirely and report failure; clamping to 1 attempt is the
        # reading that keeps a malformed config from silently disabling every
        # API call.
        self.max_retries = max(1, max_retries)
        self.retry_delay = max(0, retry_delay)
        self.timeout = timeout
        self.user_agent = user_agent
        self._sleep = sleep if sleep is not None else time.sleep
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._on_retry = on_retry

    # ------------------------------------------------------------------
    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        data: Optional[bytes] = None,
        description: str = "",
    ) -> Response:
        """One request, retried per :meth:`_is_retryable_status` and transport.

        Raises :class:`HttpStatusError` or :class:`HttpTransportError`; never
        returns a response the caller has to check the status of, because that
        check is exactly what the bash forgot to do.
        """
        what = description or redact_url(url)
        attempt = 1
        delay = self.retry_delay
        last: HttpError

        while True:
            try:
                return self._attempt(url, method, headers, data)
            except HttpStatusError as exc:
                last = exc
                if not _is_retryable_status(exc.status):
                    raise
            except HttpTransportError as exc:
                last = exc

            if attempt >= self.max_retries:
                raise last

            if self._on_retry is not None:
                self._on_retry(
                    "attempt {0}/{1} failed for {2}: {3}. Retrying in {4}s".format(
                        attempt, self.max_retries, what, last, delay
                    )
                )
            if delay:
                self._sleep(delay)
            attempt += 1
            delay *= 2  # exponential backoff, as retry_command does

    # ------------------------------------------------------------------
    def _attempt(
        self,
        url: str,
        method: str,
        headers: Optional[Mapping[str, str]],
        data: Optional[bytes],
    ) -> Response:
        request_headers: Dict[str, str] = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)

        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            handle = self._opener(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a response object, so the body is readable
            # and is where both services put the reason.
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - a body that will not read
                body = b""
            raise HttpStatusError(exc.code, exc.reason or "", url, body)
        except urllib.error.URLError as exc:
            raise HttpTransportError(
                "{0} for {1}".format(exc.reason, redact_url(url)), url
            )
        except socket.timeout:
            # 3.9 raises socket.timeout here rather than the TimeoutError that
            # 3.10+ aliases it to, and it is not a URLError either.
            raise HttpTransportError(
                "timed out after {0}s for {1}".format(self.timeout, redact_url(url)), url
            )

        with handle:
            body = handle.read()
            status = getattr(handle, "status", None)
            if status is None:  # pragma: no cover - urllib < 3.9 shape
                status = handle.getcode()
            headers_out = dict(handle.headers.items()) if handle.headers else {}

        if not 200 <= status < 300:
            # urlopen only raises for >=400, so a 3xx that was not followed
            # lands here rather than in the except above.
            raise HttpStatusError(status, "", url, body)

        return Response(status, headers_out, body, url)

    # ------------------------------------------------------------------
    def get_json(self, url: str, description: str = "") -> Any:
        return self.request(url, description=description).json()


def build_url(base: str, params: Mapping[str, str]) -> str:
    """A URL with the parameters percent-encoded the way the bash encoded them.

    The bash pipes the query through ``jq -sRr @uri``, which escapes everything
    outside the unreserved set ``A-Za-z0-9-_.~``.  ``quote_via=quote`` with an
    empty ``safe`` is the same set — notably it encodes a space as ``%20``
    rather than the ``+`` that ``urlencode`` would produce by default, and TMDb
    treats those differently for a title containing a literal plus.

    Parameters with an empty value are dropped, matching the bash, which
    appends ``&year=`` only inside ``if [ -n "$year" ]``.

    ``errors="surrogateescape"`` is not optional.  ``parse_media_info`` decodes
    names with ``surrogateescape`` — put.io has produced filenames that are not
    valid UTF-8, and the whole ``junk``/``classify`` path carries them through
    unchanged for that reason.  A title lifted out of one of those reaches here
    holding lone surrogates, and ``quote`` under the default strict errors
    raises ``UnicodeEncodeError``.  That is not an :class:`HttpError`, so a
    caller's per-item ``except HttpError`` does not catch it and one bad name
    takes the whole batch down.  With surrogateescape the original bytes come
    back out as ordinary percent-escapes (``caf\\udce9`` -> ``caf%E9``) and only
    that one lookup can fail, on TMDb's answer rather than on our own encoder.
    """
    query = urllib.parse.urlencode(
        [(k, v) for k, v in params.items() if v != ""],
        quote_via=urllib.parse.quote,
        safe="",
        errors="surrogateescape",
    )
    return "{0}?{1}".format(base, query) if query else base

