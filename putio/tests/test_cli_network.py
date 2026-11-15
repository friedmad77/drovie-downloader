"""``plex-scan`` and ``tmdb-search`` — the output contract with bash.

``plex-scan`` is the one call site this phase switches over, and its contract
has a sharp edge worth stating up front: the processor falls back to its own
``scan_plex_library`` when this command **did not run**, so the command must
never fail *after* triggering a scan, or the scan fires twice.  The ``DONE``
sentinel is what draws that line, and most of what follows is pinning it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from putio.__main__ import EXIT_FAIL, EXIT_OK, main
from putio.tests.fake_http import FakeOpener, FakeResponse, fixture, http_error

#: RATE_LIMIT_FILE and RATE_LIMIT_DELAY are pinned here for two reasons, and
#: the second one matters more.  Config defaults RATE_LIMIT_FILE to
#: /tmp/tmdb_rate_limit — the *live* window on this machine — so a suite that
#: left it alone would move a production counter every time it ran.  And the
#: shipped RATE_LIMIT_DELAY of 1 would make each search in these tests really
#: sleep a second.
_RATE_DIR = tempfile.mkdtemp(prefix="putio-test-rate-")

ENV = {
    "LOCAL_DIR": "/tmp/putio-test",
    "PLEX_URL": "http://localhost:32400",
    "PLEX_TOKEN": "test-token",
    "MAX_RETRIES": "1",
    "RETRY_DELAY": "0",
    "RATE_LIMIT_FILE": os.path.join(_RATE_DIR, "window"),
    "RATE_LIMIT_DELAY": "0",
}


def run(argv, env=None, outcomes=None, stdin=""):
    """Run a subcommand with the network replaced by recorded responses."""
    opener = FakeOpener(outcomes if outcomes is not None else FakeResponse(b""))
    out, err = io.StringIO(), io.StringIO()
    merged = dict(ENV)
    merged.update(env or {})
    with mock.patch.dict(os.environ, merged, clear=True):
        with mock.patch("urllib.request.urlopen", opener):
            with mock.patch("sys.stdin", io.StringIO(stdin)):
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(argv)
    return code, out.getvalue(), err.getvalue(), opener


def lines(text):
    """Parse the ``LEVEL<TAB>message`` protocol back into pairs."""
    parsed = []
    for line in text.splitlines():
        level, _, message = line.partition("\t")
        parsed.append((level, message))
    return parsed


def messages(text):
    return [message for _, message in lines(text)]


class TestPlexScanProtocol(unittest.TestCase):
    def test_every_line_is_level_tab_message(self):
        """bash reads these with `read -r lvl msg`, so the shape is load-bearing."""
        _, out, _, _ = run(["plex-scan", "--movie", "1", "--webhook-delay", "0"])
        for line in out.splitlines():
            self.assertIn("\t", line)
            level = line.split("\t")[0]
            self.assertIn(level, {"DEBUG", "INFO", "WARN", "ERROR", "DONE"})

    def test_done_is_the_last_line_on_the_happy_path(self):
        _, out, _, _ = run(["plex-scan", "--movie", "1", "--webhook-delay", "0"])
        self.assertEqual(lines(out)[-1][0], "DONE")

    def test_a_message_containing_a_newline_cannot_desynchronise_the_reader(self):
        """An error body reaches the log; a body with a newline in it would
        otherwise put its tail on a line of its own, and bash would read that
        fragment as a level."""
        _, out, _, _ = run(
            ["plex-scan", "--movie", "1", "--webhook-delay", "0"],
            outcomes=http_error("http://x", 500, b"line one\nline two\nline three"),
        )
        for line in out.splitlines():
            self.assertEqual(len(line.split("\t")), 2, line)

    def test_nothing_requested_is_still_handled(self):
        """Falling back to bash here would scan libraries nothing was added to."""
        code, out, _, opener = run(["plex-scan"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(lines(out)[-1][0], "DONE")
        self.assertEqual(opener.count, 0)


class TestPlexScanWording(unittest.TestCase):
    """The log lines are a transcription; a run must read the same as before."""

    def test_a_successful_scan_says_what_the_bash_said(self):
        _, out, _, _ = run(["plex-scan", "--movie", "1", "--webhook-delay", "0"])
        said = messages(out)
        self.assertIn("[PLEX] Triggering scan for movie library (ID: 1)...", said)
        self.assertIn("[PLEX] movie library scan triggered successfully", said)

    def test_a_missing_token_says_what_the_bash_said_and_stops(self):
        code, out, _, opener = run(["plex-scan", "--movie", "1"], {"PLEX_TOKEN": ""})
        self.assertEqual(code, EXIT_OK)
        self.assertIn("[PLEX] PLEX_TOKEN not set, skipping scan", messages(out))
        self.assertEqual(opener.count, 0)

    def test_discovery_is_announced_and_used(self):
        """An empty id means "find it", which is what an unpinned config gives."""
        _, out, _, opener = run(
            ["plex-scan", "--tv", "", "--webhook-delay", "0"],
            outcomes=[FakeResponse(fixture("plex_sections.xml")), FakeResponse(b"")],
        )
        said = messages(out)
        self.assertIn("[PLEX] Detecting library ID for tv...", said)
        self.assertIn("[PLEX] Triggering scan for tv library (ID: 2)...", said)
        self.assertIn("/library/sections/2/refresh", opener.last.url)

    def test_discovery_failure_says_both_lines_the_bash_said(self):
        empty = b'<MediaContainer size="0"></MediaContainer>'
        _, out, _, _ = run(["plex-scan", "--movie", ""], outcomes=FakeResponse(empty))
        said = messages(out)
        self.assertIn("[PLEX] Could not find library ID for type 'movie'", said)
        self.assertIn("[PLEX] Could not determine library ID for movie", said)


class TestPlexScanFailures(unittest.TestCase):
    def test_a_404_is_reported_as_a_failure_not_a_success(self):
        """The headline fix: under the bash this logged "triggered successfully"."""
        code, out, _, _ = run(
            ["plex-scan", "--movie", "1"],
            outcomes=http_error("http://x", 404, fixture("plex_refresh_404.html")),
        )
        said = messages(out)
        self.assertEqual(code, EXIT_OK)  # handled — bash must not redo it
        self.assertIn("[PLEX] Failed to trigger movie library scan", said)
        self.assertNotIn("[PLEX] movie library scan triggered successfully", said)
        self.assertTrue(any("404" in m for m in said))

    def test_a_failed_movie_scan_does_not_stop_the_tv_scan(self):
        """Two libraries in one call must fail independently, as two separate
        calls to scan_plex_library did."""
        code, out, _, opener = run(
            ["plex-scan", "--movie", "1", "--tv", "2", "--webhook-delay", "0"],
            outcomes=[http_error("http://x", 500, b"boom"), FakeResponse(b"")],
        )
        self.assertEqual(code, EXIT_OK)
        said = messages(out)
        self.assertIn("[PLEX] Failed to trigger movie library scan", said)
        self.assertIn("[PLEX] tv library scan triggered successfully", said)

    def test_an_unreachable_plex_is_reported_and_still_handled(self):
        code, out, _, _ = run(
            ["plex-scan", "--movie", "1"], outcomes=urllib.error.URLError("refused")
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("[PLEX] Failed to trigger movie library scan", messages(out))
        self.assertEqual(lines(out)[-1][0], "DONE")

    def test_a_crash_before_any_request_asks_bash_to_take_over(self):
        """Nothing happened, so bash re-doing it is safe — and better than a
        run that quietly never scans."""
        with mock.patch("putio.__main__.PlexClient", side_effect=RuntimeError("boom")):
            code, out, _, _ = run(["plex-scan", "--movie", "1"])
        self.assertEqual(code, EXIT_FAIL)
        self.assertNotIn("DONE", [level for level, _ in lines(out)])

    def test_a_crash_during_discovery_asks_bash_to_take_over(self):
        """Discovery is a GET — it triggers nothing, so a crash there is safe
        for bash to redo, and redoing it is the only way the library gets
        scanned at all this run."""
        with mock.patch("putio.plex.PlexClient.sections", side_effect=RuntimeError("boom")):
            code, out, _, _ = run(["plex-scan", "--movie", ""])
        self.assertEqual(code, EXIT_FAIL)
        self.assertNotIn("DONE", [level for level, _ in lines(out)])

    def test_a_crash_after_a_scan_does_not_ask_bash_to_take_over(self):
        """Because bash would then trigger the same scan a second time."""
        with mock.patch("putio.__main__._fire_webhook", side_effect=RuntimeError("boom")):
            code, out, _, _ = run(["plex-scan", "--movie", "1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(lines(out)[-1][0], "DONE")
        self.assertTrue(any("Unexpected failure" in m for m in messages(out)))


class TestWebhookOrdering(unittest.TestCase):
    HOOK = {
        "PLEX_REQUEST_WEBHOOK_URL": "https://example.invalid/api/webhooks/plex-check",
        "PLEX_REQUEST_WEBHOOK_KEY": "hunter2",
    }

    def test_it_fires_after_a_successful_scan(self):
        _, out, _, opener = run(
            ["plex-scan", "--movie", "1", "--webhook-delay", "0"], self.HOOK
        )
        self.assertEqual(opener.count, 2)
        self.assertIn("plex-check", opener.last.url)
        self.assertIn(
            "[WEBHOOK] Drovie availability check triggered successfully", messages(out)
        )

    def test_it_does_not_fire_after_a_failed_scan(self):
        _, _, _, opener = run(
            ["plex-scan", "--movie", "1", "--webhook-delay", "0"],
            self.HOOK,
            outcomes=http_error("http://x", 404, b""),
        )
        self.assertEqual(opener.count, 1)

    def test_it_fires_once_per_successful_scan(self):
        """QUIRK, kept: the bash calls the webhook from inside
        scan_plex_library, so a run that added both a film and an episode pokes
        Drovie twice and waits ten seconds each time.  Collapsing that to
        one call would be an improvement and is not this phase's to make."""
        _, _, _, opener = run(
            ["plex-scan", "--movie", "1", "--tv", "2", "--webhook-delay", "0"], self.HOOK
        )
        self.assertEqual(opener.count, 4)  # two refreshes, two webhooks

    def test_a_webhook_refusal_is_a_warning_not_a_failure(self):
        _, out, _, _ = run(
            ["plex-scan", "--movie", "1", "--webhook-delay", "0"],
            self.HOOK,
            outcomes=[FakeResponse(b""), http_error("http://x", 403, b"denied")],
        )
        said = messages(out)
        self.assertIn(
            "[WEBHOOK] Failed to trigger Drovie availability check", said
        )
        self.assertIn("[PLEX] movie library scan triggered successfully", said)

    def test_it_is_skipped_when_only_half_configured(self):
        """`if [ -n "$URL" ] && [ -n "$KEY" ]` — both, or neither."""
        for partial in ({"PLEX_REQUEST_WEBHOOK_URL": self.HOOK["PLEX_REQUEST_WEBHOOK_URL"]},
                        {"PLEX_REQUEST_WEBHOOK_KEY": "hunter2"}):
            _, _, _, opener = run(["plex-scan", "--movie", "1"], partial)
            self.assertEqual(opener.count, 1, partial)

    def test_the_ten_second_wait_is_the_default(self):
        """The flag exists for the harness; production must not need it."""
        waits = []
        with mock.patch("putio.__main__.time.sleep", waits.append):
            run(["plex-scan", "--movie", "1"], self.HOOK)
        self.assertEqual(waits, [10])


class TestSecrets(unittest.TestCase):
    def test_no_token_or_key_reaches_stdout_even_when_everything_fails(self):
        env = {
            "PLEX_TOKEN": "plex-secret-value",
            "PLEX_REQUEST_WEBHOOK_URL": "https://example.invalid/hook",
            "PLEX_REQUEST_WEBHOOK_KEY": "webhook-secret-value",
        }
        _, out, err, _ = run(
            ["plex-scan", "--movie", "", "--webhook-delay", "0"],
            env,
            outcomes=http_error("http://x?X-Plex-Token=plex-secret-value", 401, b"no"),
        )
        self.assertNotIn("plex-secret-value", out + err)
        self.assertNotIn("webhook-secret-value", out + err)

    def test_a_tmdb_key_never_reaches_the_output(self):
        _, out, err, _ = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "tmdb-secret-value"},
            outcomes=http_error(
                "https://api.themoviedb.org/3/search/movie?api_key=tmdb-secret-value",
                401,
                fixture("tmdb_invalid_key.json"),
            ),
            stdin="Heat\n",
        )
        self.assertNotIn("tmdb-secret-value", out + err)


class TestTmdbSearchCommand(unittest.TestCase):
    """Batched by construction — there is no single-title form to misuse."""

    def test_a_batch_gets_one_json_object_per_line(self):
        code, out, _, _ = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "k"},
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
            stdin="Blade Runner 2049\t2017\nHeat\t1995\n",
        )
        self.assertEqual(code, EXIT_OK)
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["query"], "Blade Runner 2049")
        self.assertEqual(records[0]["year"], "2017")
        self.assertEqual(records[0]["match"]["title"], "Blade Runner 2049")

    def test_one_interpreter_start_serves_the_whole_batch(self):
        """The point of the shape: three titles, one process, three requests."""
        _, out, _, opener = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "k"},
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
            stdin="A\nB\nC\n",
        )
        self.assertEqual(opener.count, 3)
        self.assertEqual(len(out.splitlines()), 3)

    def test_no_results_is_a_null_match_not_a_missing_line(self):
        """So a caller can zip the output against its input."""
        _, out, _, _ = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "k"},
            outcomes=FakeResponse(fixture("tmdb_movie_no_results.json")),
            stdin="nothing\n",
        )
        self.assertIsNone(json.loads(out)["match"])

    def test_a_failed_lookup_is_reported_per_line_and_in_the_exit_code(self):
        code, out, _, _ = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "k"},
            outcomes=http_error("https://x", 401, fixture("tmdb_invalid_key.json")),
            stdin="Heat\n",
        )
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("Invalid API key", json.loads(out)["error"])

    def test_the_tv_flag_selects_the_other_endpoint(self):
        _, _, _, opener = run(
            ["tmdb-search", "--tv"],
            {"TMDB_API_KEY": "k"},
            outcomes=FakeResponse(fixture("tmdb_tv_search.json")),
            stdin="Severance\t2022\n",
        )
        self.assertIn("/search/tv?", opener.last.url)

    def test_a_missing_key_fails_before_any_request(self):
        code, _, err, opener = run(
            ["tmdb-search"], {"TMDB_API_KEY": ""}, stdin="Heat\n"
        )
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("TMDB_API_KEY", err)
        self.assertEqual(opener.count, 0)

    def test_blank_lines_are_skipped_rather_than_searched_for(self):
        _, _, _, opener = run(
            ["tmdb-search"],
            {"TMDB_API_KEY": "k"},
            outcomes=FakeResponse(fixture("tmdb_movie_search.json")),
            stdin="Heat\n\n   \nCollateral\n",
        )
        self.assertEqual(opener.count, 2)


if __name__ == "__main__":
    unittest.main()
