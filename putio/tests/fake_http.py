"""A stand-in for ``urllib.request.urlopen``, driven by recorded responses.

Nothing in this suite opens a socket.  The alternative — hitting TMDb and a
local Plex — makes the tests fail when the network is slow, when the key is
rotated and on a machine that is not this one, and a suite that fails for
reasons unrelated to the code is a suite nobody runs.

:class:`FakeOpener` is deliberately shaped like the real thing rather than like
a mock: it is handed to :class:`~putio.http.HttpClient` through the same
``opener`` parameter production would use, so the code under test takes the
same path, including the ``with`` block and the ``HTTPError``-carries-a-body
subtlety that a naive mock would paper over.
"""

from __future__ import annotations

import io
import pathlib
import urllib.error
from email.message import Message
from typing import Any, Dict, List, Optional, Union

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _headers(pairs: Optional[Dict[str, str]] = None) -> Message:
    """urllib hands back an ``email.message.Message``, so do the same."""
    message = Message()
    for key, value in (pairs or {}).items():
        message[key] = value
    return message


class FakeResponse(io.BytesIO):
    """What ``urlopen`` returns on a 2xx: a readable, closable file object."""

    def __init__(self, body: bytes, status: int = 200, headers: Optional[Dict[str, str]] = None):
        super().__init__(body)
        self.status = status
        self.headers = _headers(headers)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def http_error(url: str, status: int, body: bytes, reason: str = "") -> urllib.error.HTTPError:
    """An ``HTTPError`` with a readable body, which is where the reason lives.

    ``urlopen`` raises these for any status at or above 400, and the object is
    both the exception and the response — reading it is how
    :mod:`putio.http` recovers TMDb's ``status_message`` and Plex's HTML
    ``<title>``.
    """
    return urllib.error.HTTPError(url, status, reason, _headers(), io.BytesIO(body))


class Call:
    """One request the code under test made."""

    __slots__ = ("url", "method", "headers", "data", "timeout")

    def __init__(self, url: str, method: str, headers: Dict[str, str], data, timeout) -> None:
        self.url = url
        self.method = method
        self.headers = headers
        self.data = data
        self.timeout = timeout

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Call({0} {1})".format(self.method, self.url)


class FakeOpener:
    """Replays a scripted sequence of outcomes and records what was asked.

    ``outcomes`` is a list of either a :class:`FakeResponse`, an exception to
    raise, or a callable taking the :class:`Call`.  The last entry repeats once
    the list is exhausted, so a test that only cares about the first request
    does not have to spell out how many retries it expects.
    """

    def __init__(self, outcomes: Union[Any, List[Any]]) -> None:
        self.outcomes = outcomes if isinstance(outcomes, list) else [outcomes]
        self.calls: List[Call] = []

    def __call__(self, request, timeout=None):
        # urllib's Request normalises header names to Title-Case; keep that so
        # a test asserting on a header is asserting on what the server sees.
        self.calls.append(
            Call(
                request.full_url,
                request.get_method(),
                dict(request.header_items()),
                request.data,
                timeout,
            )
        )

        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]

        if callable(outcome) and not isinstance(outcome, FakeResponse):
            outcome = outcome(self.calls[-1])
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, FakeResponse):
            # A response object is consumed by reading it, so a repeated
            # outcome has to be handed out fresh each time.
            return FakeResponse(outcome.getvalue(), outcome.status)
        raise AssertionError("unusable outcome: {0!r}".format(outcome))

    # ------------------------------------------------------------------
    @property
    def last(self) -> Call:
        return self.calls[-1]

    @property
    def count(self) -> int:
        return len(self.calls)


class RecordingSleep:
    """A ``sleep`` that records instead of sleeping.

    The retry path is 5s → 10s → 20s and the webhook waits ten seconds; a suite
    that actually waited would take most of a minute to prove arithmetic.
    """

    def __init__(self) -> None:
        self.waits: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
