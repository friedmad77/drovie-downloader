"""Plex discovery and scan triggering, against a recorded sections response.

The interesting tests here are the two that pin behaviour the bash gets wrong:
a scan that Plex refused must be reported as refused, and library discovery
must not depend on how the XML happens to be wrapped.
"""

from __future__ import annotations

import unittest
import urllib.error

from putio.http import HttpClient, HttpError, HttpStatusError, HttpTransportError
from putio.plex import SECTION_TYPES, PlexClient
from putio.tests.fake_http import FakeOpener, FakeResponse, RecordingSleep, fixture, http_error

BASE = "http://localhost:32400"
TOKEN = "test-token-not-a-real-one"


def client(outcomes, base=BASE):
    opener = FakeOpener(outcomes)
    http = HttpClient(opener=opener, sleep=RecordingSleep(), max_retries=1)
    return PlexClient(base, TOKEN, http), opener


class TestSections(unittest.TestCase):
    def test_it_reads_the_real_response(self):
        plex, _ = client(FakeResponse(fixture("plex_sections.xml")))
        sections = plex.sections()
        self.assertEqual(
            [(s.key, s.type, s.title) for s in sections],
            [("1", "movie", "Movies"), ("2", "show", "TV Shows")],
        )

    def test_library_id_finds_each_type(self):
        plex, _ = client(FakeResponse(fixture("plex_sections.xml")))
        self.assertEqual(plex.library_id("movie"), "1")
        self.assertEqual(plex.library_id("show"), "2")

    def test_the_processor_calls_a_tv_library_a_show(self):
        """`get_plex_library_id "show"` — the one place the names differ."""
        self.assertEqual(SECTION_TYPES["tv"], "show")
        self.assertEqual(SECTION_TYPES["movie"], "movie")

    def test_a_type_with_no_library_is_none_not_a_guess(self):
        plex, _ = client(FakeResponse(fixture("plex_sections.xml")))
        self.assertIsNone(plex.library_id("artist"))

    def test_it_does_not_care_how_the_xml_is_wrapped(self):
        """The bash's grep pipeline does, and gets it wrong.

        Fold the real response onto one line — a legal serialisation, and what
        a proxy in front of Plex may hand back — and
        ``get_plex_library_id show`` returns "1\\n2", two ids joined by a
        newline, which then goes straight into the refresh URL.  Measured
        against this same fixture, not theorised.
        """
        one_line = fixture("plex_sections.xml").replace(b"\n", b"")
        plex, _ = client(FakeResponse(one_line))
        self.assertEqual(plex.library_id("show"), "2")

    def test_document_order_decides_between_two_libraries_of_a_type(self):
        """`head -1` in the bash; first match here.  Same answer either way."""
        xml = (
            b'<MediaContainer size="2">'
            b'<Directory key="7" type="movie" title="Films" />'
            b'<Directory key="9" type="movie" title="More Films" />'
            b"</MediaContainer>"
        )
        plex, _ = client(FakeResponse(xml))
        self.assertEqual(plex.library_id("movie"), "7")

    def test_a_bad_token_raises_rather_than_reading_as_an_empty_library(self):
        """Plex answers 401 with an HTML page and `curl -s` exits 0 on it."""
        plex, _ = client(http_error(BASE, 401, fixture("plex_sections_401.html")))
        with self.assertRaises(HttpStatusError) as caught:
            plex.library_id("movie")
        self.assertEqual(caught.exception.status, 401)

    def test_a_200_that_is_not_xml_raises_httperror(self):
        """A reverse proxy returning its own page with a 200."""
        plex, _ = client(FakeResponse(b"<html>not xml at all"))
        with self.assertRaises(HttpError):
            plex.sections()

    def test_a_directory_with_no_key_is_skipped_not_crashed_on(self):
        xml = b'<MediaContainer><Directory type="movie" /><Directory key="3" type="movie" /></MediaContainer>'
        plex, _ = client(FakeResponse(xml))
        self.assertEqual(plex.library_id("movie"), "3")


class TestRefresh(unittest.TestCase):
    def test_a_successful_refresh_posts_to_the_right_place(self):
        plex, opener = client(FakeResponse(b""))
        plex.refresh("1")
        self.assertEqual(opener.last.method, "POST")
        self.assertTrue(opener.last.url.startswith(BASE + "/library/sections/1/refresh?"))
        self.assertEqual(opener.last.data, b"")

    def test_a_404_raises(self):
        """The bug this phase removes.

        `curl -s -X POST` exits 0 on Plex's 404, so `scan_plex_library` logs
        "library scan triggered successfully" for a section that does not
        exist.  Recorded from the live server: a real 404, in HTML.
        """
        plex, _ = client(http_error(BASE, 404, fixture("plex_refresh_404.html")))
        with self.assertRaises(HttpStatusError) as caught:
            plex.refresh("9999")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("Not Found", caught.exception.excerpt)

    def test_an_unreachable_plex_raises(self):
        plex, _ = client(urllib.error.URLError("connection refused"))
        with self.assertRaises(HttpTransportError):
            plex.refresh("1")

    def test_a_trailing_slash_in_plex_url_does_not_double_up(self):
        plex, opener = client(FakeResponse(b""), base=BASE + "/")
        plex.refresh("1")
        self.assertNotIn("//library", opener.last.url)


class TestWebhook(unittest.TestCase):
    def test_the_key_goes_in_the_header_the_bash_uses(self):
        plex, opener = client(FakeResponse(b""))
        plex.trigger_webhook("https://example.invalid/api/webhooks/plex-check", "hunter2")
        self.assertEqual(opener.last.method, "POST")
        # urllib title-cases header names on the way out.
        self.assertEqual(opener.last.headers.get("X-webhook-key"), "hunter2")

    def test_a_refusal_raises(self):
        plex, _ = client(http_error("https://example.invalid/hook", 403, b"nope"))
        with self.assertRaises(HttpStatusError):
            plex.trigger_webhook("https://example.invalid/hook", "hunter2")


class TestTokenHandling(unittest.TestCase):
    def test_the_token_never_appears_in_an_error_message(self):
        """Errors here are logged verbatim, and the log is kept for weeks."""
        plex, _ = client(http_error(BASE, 401, b""))
        with self.assertRaises(HttpStatusError) as caught:
            plex.sections()
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_the_token_is_sent_as_the_query_parameter_plex_expects(self):
        plex, opener = client(FakeResponse(fixture("plex_sections.xml")))
        plex.sections()
        self.assertIn("X-Plex-Token=" + TOKEN, opener.last.url)


if __name__ == "__main__":
    unittest.main()
