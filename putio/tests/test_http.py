"""The HTTP layer: status handling, retries, redaction.

The theme running through these is that ``curl -s`` could not see a status
code, so the bash treated every reachable server as a working one.  Each test
here pins one of the answers that were previously indistinguishable.
"""

from __future__ import annotations

import socket
import unittest
import urllib.error

from putio.http import (
    HttpClient,
    HttpError,
    HttpStatusError,
    HttpTransportError,
    build_url,
    redact_url,
)
from putio.tests.fake_http import FakeOpener, FakeResponse, RecordingSleep, fixture, http_error

URL = "https://example.invalid/thing"


def client(outcomes, **kwargs):
    sleep = RecordingSleep()
    opener = FakeOpener(outcomes)
    return (
        HttpClient(opener=opener, sleep=sleep, **kwargs),
        opener,
        sleep,
    )


class TestRedaction(unittest.TestCase):
    """No log line may carry a key, and every error message names a URL."""

    def test_tmdb_key_is_replaced(self):
        out = redact_url("https://api.themoviedb.org/3/search/movie?api_key=abc123&query=Heat")
        self.assertNotIn("abc123", out)
        self.assertIn("api_key=<redacted>", out)
        self.assertIn("query=Heat", out)

    def test_plex_token_is_replaced_whatever_its_case(self):
        for name in ("X-Plex-Token", "x-plex-token", "X-PLEX-TOKEN"):
            out = redact_url("http://localhost:32400/library/sections?{0}=sekret".format(name))
            self.assertNotIn("sekret", out, name)

    def test_an_empty_value_is_left_alone(self):
        """`?api_key=` says the key is missing, which is worth seeing."""
        self.assertEqual(redact_url("http://x/?api_key="), "http://x/?api_key=")

    def test_an_error_body_that_echoes_the_url_is_redacted_too(self):
        """The excerpt is server-controlled text on its way into the log.

        A proxy that cannot reach TMDb names the request it failed on, so
        redacting the URL the request went to is no use if the body quotes it
        straight back.  All three excerpt shapes go through the same guard.
        """
        secret = "REAL_TMDB_KEY_abc123"
        url = "https://api.themoviedb.org/3/search/movie?api_key={0}&query=Heat".format(secret)
        bodies = [
            ("plain text", "could not connect upstream for {0}".format(url).encode()),
            ("json message", '{{"message":"upstream failure for {0}"}}'.format(url).encode()),
            ("html title", "<html><head><title>502 for {0}</title></head>".format(url).encode()),
        ]
        for label, body in bodies:
            http, _, _ = client(http_error(url, 502, body))
            with self.assertRaises(HttpStatusError) as caught:
                http.request(url)
            self.assertNotIn(secret, str(caught.exception), label)
            self.assertNotIn(secret, caught.exception.excerpt, label)

    def test_errors_never_carry_the_key(self):
        """The redaction has to be in the exception, not at each log site.

        Every ``except HttpError as exc: log(exc)`` in the codebase would
        otherwise be one missed call away from writing a key to a file that is
        kept for weeks and read by putioctl.
        """
        secret = "SUPERSECRETKEY"
        url = "https://api.themoviedb.org/3/search/movie?api_key={0}&query=Heat".format(secret)
        http, _, _ = client(http_error(url, 401, fixture("tmdb_invalid_key.json")))
        with self.assertRaises(HttpStatusError) as caught:
            http.request(url)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, caught.exception.url)


class TestStatusHandling(unittest.TestCase):
    def test_a_401_raises_and_carries_tmdbs_own_message(self):
        """The message the bash threw away.

        `jq -r '.results[0].title // empty'` reads an empty title out of this
        body, so the caller logged "Lookup failed" and moved the file to
        Unmatched — with nothing anywhere naming a revoked key.
        """
        http, _, _ = client(http_error(URL, 401, fixture("tmdb_invalid_key.json")))
        with self.assertRaises(HttpStatusError) as caught:
            http.request(URL)
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("Invalid API key", str(caught.exception))

    def test_a_plex_404_raises_and_carries_the_html_title(self):
        """`curl -s` exits 0 here, which is why every scan was 'successful'."""
        http, _, _ = client(http_error(URL, 404, fixture("plex_refresh_404.html")))
        with self.assertRaises(HttpStatusError) as caught:
            http.request(URL, method="POST", data=b"")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("Not Found", caught.exception.excerpt)
        self.assertNotIn("<html>", caught.exception.excerpt)

    def test_a_200_returns_the_body(self):
        http, _, _ = client(FakeResponse(b'{"ok":true}'))
        self.assertEqual(http.request(URL).json(), {"ok": True})

    def test_a_200_that_is_not_json_raises_httperror_not_valueerror(self):
        """A captive portal and a proxy error page both look like this.

        It has to arrive as HttpError so the single ``except`` at the call site
        catches it; a bare ValueError would escape and kill the run.
        """
        http, _, _ = client(FakeResponse(b"<html>nope</html>"))
        with self.assertRaises(HttpError):
            http.request(URL).json()


class TestRetryPolicy(unittest.TestCase):
    """Same shape as ``retry_command``: N attempts, doubling delay."""

    def test_transport_errors_retry_with_the_bash_backoff(self):
        http, opener, sleep = client(
            urllib.error.URLError("connection refused"), max_retries=3, retry_delay=5
        )
        with self.assertRaises(HttpTransportError):
            http.request(URL)
        self.assertEqual(opener.count, 3)
        self.assertEqual(sleep.waits, [5, 10])  # 5 → 10, then give up

    def test_a_socket_timeout_is_a_transport_error(self):
        """3.9 raises socket.timeout here, and it is not a URLError.

        On 3.10+ the same thing is a TimeoutError alias, so the branch that
        catches it is only exercised on the older interpreter — which is
        exactly the one a machine without the PYTHON_BIN pin runs.
        """
        http, _, _ = client(socket.timeout("timed out"), max_retries=1)
        with self.assertRaises(HttpTransportError):
            http.request(URL)

    def test_a_500_is_retried(self):
        http, opener, _ = client(http_error(URL, 500, b"boom"), max_retries=3)
        with self.assertRaises(HttpStatusError):
            http.request(URL)
        self.assertEqual(opener.count, 3)

    def test_a_429_is_retried(self):
        http, opener, _ = client(http_error(URL, 429, b""), max_retries=2)
        with self.assertRaises(HttpStatusError):
            http.request(URL)
        self.assertEqual(opener.count, 2)

    def test_a_401_is_not_retried(self):
        """Retrying a revoked key three times only delays the message."""
        http, opener, sleep = client(http_error(URL, 401, b""), max_retries=3)
        with self.assertRaises(HttpStatusError):
            http.request(URL)
        self.assertEqual(opener.count, 1)
        self.assertEqual(sleep.waits, [])

    def test_a_retry_that_succeeds_returns_the_body(self):
        http, opener, _ = client(
            [urllib.error.URLError("flap"), FakeResponse(b'{"ok":1}')], max_retries=3
        )
        self.assertEqual(http.request(URL).json(), {"ok": 1})
        self.assertEqual(opener.count, 2)

    def test_retries_are_announced_to_the_caller(self):
        """So the log keeps the ``[RETRY]`` lines retry_command wrote."""
        seen = []
        sleep = RecordingSleep()
        http = HttpClient(
            opener=FakeOpener(urllib.error.URLError("nope")),
            sleep=sleep,
            max_retries=2,
            on_retry=seen.append,
        )
        with self.assertRaises(HttpTransportError):
            http.request(URL, description="Plex sections API")
        self.assertEqual(len(seen), 1)
        self.assertIn("Plex sections API", seen[0])

    def test_zero_retries_still_makes_one_attempt(self):
        """`MAX_RETRIES=0` must not silently disable every API call.

        Bash's `while [ $attempt -le $max_attempts ]` would skip the command
        entirely; one attempt is the reading that keeps a typo in the config
        from turning off TMDb.
        """
        http, opener, _ = client(FakeResponse(b"{}"), max_retries=0)
        http.request(URL)
        self.assertEqual(opener.count, 1)


class TestRequestShape(unittest.TestCase):
    def test_a_timeout_is_always_passed(self):
        """curl passed no --max-time; a hung Plex held the lockfile forever."""
        http, opener, _ = client(FakeResponse(b"{}"))
        http.request(URL)
        self.assertIsNotNone(opener.last.timeout)
        self.assertGreater(opener.last.timeout, 0)

    def test_a_post_with_no_body_still_sends_content_length_zero(self):
        """What `curl -X POST` sends, and what a proxy may insist on."""
        http, opener, _ = client(FakeResponse(b""))
        http.request(URL, method="POST", data=b"")
        self.assertEqual(opener.last.method, "POST")
        self.assertEqual(opener.last.data, b"")

    def test_a_user_agent_is_set(self):
        http, opener, _ = client(FakeResponse(b"{}"))
        http.request(URL)
        self.assertNotIn("urllib", opener.last.headers.get("User-agent", ""))


class TestBuildUrl(unittest.TestCase):
    """Must encode exactly as ``jq -sRr @uri`` did."""

    def test_spaces_become_percent_twenty_not_plus(self):
        url = build_url("https://x/s", {"query": "Blade Runner 2049"})
        self.assertIn("query=Blade%20Runner%202049", url)

    def test_a_literal_plus_survives(self):
        """`urlencode`'s default would turn this into a space at the server."""
        url = build_url("https://x/s", {"query": "C++"})
        self.assertIn("query=C%2B%2B", url)

    def test_unreserved_characters_are_left_alone(self):
        """jq's @uri leaves A-Za-z0-9-_.~ alone and so must this."""
        url = build_url("https://x/s", {"query": "a-b_c.d~e"})
        self.assertIn("query=a-b_c.d~e", url)

    def test_an_empty_parameter_is_dropped(self):
        """Mirrors `if [ -n "$year" ]` — an empty year is not sent at all."""
        url = build_url("https://x/s", {"query": "Heat", "year": ""})
        self.assertNotIn("year=", url)

    def test_a_surrogate_escaped_title_encodes_instead_of_raising(self):
        """put.io produces filenames that are not valid UTF-8.

        media.py carries them through with surrogateescape, so a title lifted
        out of one arrives here holding lone surrogates.  Under quote's default
        strict errors that is a UnicodeEncodeError — not an HttpError, so a
        caller's per-item handler misses it and one bad name kills the batch.
        """
        url = build_url("https://x/s", {"query": "latin1-caf\udce9.2001"})
        self.assertIn("query=latin1-caf%E9.2001", url)

    def test_ampersands_in_a_title_cannot_inject_a_parameter(self):
        url = build_url("https://x/s", {"query": "Fire & Ice", "api_key": "k"})
        self.assertIn("query=Fire%20%26%20Ice", url)


if __name__ == "__main__":
    unittest.main()
