"""The in-process TMDb window, including the bash quirks it reproduces.

Every assertion here that looks wrong is pinned to what ``rate_limit_tmdb``
did.  The pacing is what the operator tuned around, so a change to it would be
a behaviour change smuggled in under a refactor — see :mod:`putio.ratelimit`
for which quirks survive and which one has nothing left to reproduce now that
phase 5 has removed the state file.
"""

from __future__ import annotations

import unittest

from putio.ratelimit import NullLimiter, TokenWindow


class TestNullLimiter(unittest.TestCase):
    def test_it_does_nothing_and_says_nothing(self):
        """What a caller that wants no limiting gets, rather than a crash."""
        self.assertIsNone(NullLimiter().acquire())


class TokenWindowTest(unittest.TestCase):
    """The limiter, which since phase 5 is the only one.

    Its behaviour is the file-backed limiter's, quirks included, because the
    pacing is what the operator tuned around — see the module docstring for
    which quirks are reproduced and which one has nothing left to reproduce.
    """

    def setUp(self):
        self.now = 1000.0
        self.slept = []
        self.waits = []

    def _limiter(self, **kwargs):
        settings = dict(max_requests=3, window_seconds=10, delay=0)
        settings.update(kwargs)
        return TokenWindow(
            clock=lambda: self.now,
            sleep=self._sleep,
            on_wait=self.waits.append,
            **settings
        )

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def test_the_request_that_opens_a_window_is_not_counted(self):
        """QUIRK, and bash's: the missing-file branch writes ``0:<now>``, so a
        window really admits max+1."""
        limiter = self._limiter(max_requests=3)
        for _ in range(4):
            limiter.acquire()
        self.assertEqual(self.slept, [])

    def test_a_full_window_waits_until_it_rolls_over(self):
        limiter = self._limiter(max_requests=2)
        for _ in range(4):
            limiter.acquire()
        self.assertEqual(self.slept, [11])
        self.assertEqual(
            self.waits,
            ["[TMDB] Rate limit reached, waiting 11s before next request"],
        )

    def test_an_expired_window_starts_a_fresh_one_without_waiting(self):
        limiter = self._limiter(max_requests=1)
        limiter.acquire()
        limiter.acquire()
        self.now += 100
        limiter.acquire()
        self.assertEqual(self.slept, [])

    def test_the_delay_applies_on_the_ordinary_path_only(self):
        """QUIRK, and bash's: RATE_LIMIT_DELAY sits at the end of the
        fall-through branch, so neither the window-opening request nor one that
        waited pays it."""
        limiter = self._limiter(max_requests=5, delay=1)
        limiter.acquire()  # opens the window
        self.assertEqual(self.slept, [])
        limiter.acquire()
        self.assertEqual(self.slept, [1])

    def test_it_is_one_window_across_threads(self):
        """The whole point: bash's bucket-in-a-variable would have been a
        bucket per download subshell, each with the full allowance."""
        import threading

        limiter = TokenWindow(max_requests=1000, window_seconds=10, delay=0)
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    limiter.acquire()
            except Exception as exc:  # pragma: no cover - a race would land here
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(limiter._count, 8 * 50 - 1)  # minus the uncounted opener


class TestWaitIsVisible(unittest.TestCase):
    """A full window has to reach the log while it is waiting, not after.

    Inherited from the file-backed limiter's tests: this was the one thing
    covered there and not by TokenWindowTest, and it is the only outward sign
    that a run is being paced rather than stuck.
    """

    def test_the_message_is_emitted_before_the_sleep(self):
        order = []
        limiter = TokenWindow(
            max_requests=1,
            window_seconds=10,
            delay=0,
            clock=lambda: 1000.0,
            sleep=lambda seconds: order.append(("slept", seconds)),
            on_wait=lambda message: order.append(("said", message)),
        )
        limiter.acquire()  # opens the window, uncounted
        limiter.acquire()  # count 1
        limiter.acquire()  # full
        self.assertEqual(
            order,
            [
                ("said", "[TMDB] Rate limit reached, waiting 11s before next request"),
                ("slept", 11),
            ],
        )


if __name__ == "__main__":
    unittest.main()
