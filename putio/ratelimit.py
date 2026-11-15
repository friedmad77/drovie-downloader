"""The TMDb rate limiter — in-process, and finally only in-process.

This module's history is the port's history in miniature, so it is worth
keeping.  Bash held the window in a **file**, ``RATE_LIMIT_FILE``, in the
format ``count:window_start``.  Phases 2, 3 and 4 each proposed replacing it
with a token bucket in a variable and each declined, always for the same
reason: **more than one process in a single run made TMDb calls**, so a bucket
in a variable would have been a bucket *per process*, each with the full
35-requests-per-10-seconds allowance.

* Phase 2's version: ``download_files_parallel`` dispatched
  ``process_single_file &`` — a background subshell per download job.
* Phase 3's: one ``organize-plan`` process for the listing, and another for
  every archive that extracted.
* Phase 4 made the run one process and introduced :class:`TokenWindow` for it,
  but kept the file for two callers that could still overlap a bash writer:
  the fallback path, where bash's own ``rate_limit_tmdb`` ran, and the
  hand-run ``organize-plan`` / ``tmdb-search``, neither of which takes the
  processor's lockfile.

**Phase 5 removes the file, and here is the argument, because "the last caller
went away" is only half of it.**

Bash's ``rate_limit_tmdb`` is deleted, so the fallback writer is gone.  That
leaves the hand-run subcommands, and they are the case worth thinking about:
they still do not take the lockfile, so an operator can run ``tmdb-search``
while cron is mid-batch.  But the run itself has used :class:`TokenWindow`
since phase 4 — it does not touch the file at all — so keeping the file for
the hand-run tools would coordinate them with *nothing*.  The only window it
would still share is one hand-run invocation against another, which is not a
case anybody has, and certainly not one worth persistent state.

The honest alternatives were therefore: delete the file, or put the run back
on it so hand-run and cron really do share a window.  The second was
considered and rejected — not on cost (one lookup per downloaded file is not a
hot path, whatever else in this project is) but because the failure it would
prevent is already handled.  Exceeding TMDb's limit returns 429, and
:mod:`putio.http` has retried 429 with backoff since phase 2.  So the worst an
uncoordinated hand-run can now do is make a cron lookup wait and retry, which
is exactly what the limiter would have made it do anyway.

``RATE_LIMIT_FILE`` is dropped from ``PUTIO_PY_VARS`` and from
:class:`~putio.config.Config` with it.  Leaving the name in the handoff would
have been worse than removing it: ``config-dump`` would go on reporting a
setting that no longer does anything.  A ``.putio_config`` that still sets it
is harmless — the shim sources the file and simply does not pass it on.

**The pacing quirks are reproduced, and that is deliberate.**  They are what
the operator tuned around, so a sudden change of them would be a behaviour
change smuggled in under a refactor:

QUIRK — the request that opens a window is not counted.  Bash's "file does not
exist" branch wrote ``echo "0:$current_time"``, so a window really admits
``max_requests + 1``.  Harmless at 35.

QUIRK — only the ordinary path sleeps.  ``RATE_LIMIT_DELAY`` was applied at
the end of the fall-through branch only; the "new window" and "limit reached"
branches returned without it.  With the shipped ``RATE_LIMIT_DELAY=1`` that
alone holds the rate near 1/s, which is why the 35/10s window has almost
certainly never fired in production.

The third quirk — a malformed state resetting the window, which bash reached
by accident because ``(( current_time - window_start ))`` evaluates a
non-numeric ``window_start`` as 0 — has nothing left to reproduce it with.
There is no file to malform.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

__all__ = ["TokenWindow", "NullLimiter"]


class NullLimiter:
    """No limiting at all — for tests, and for a caller that wants none."""

    def acquire(self) -> None:
        return None


class TokenWindow:
    """A fixed window of ``max_requests`` per ``window_seconds``, in memory.

    :param delay: ``RATE_LIMIT_DELAY``, the unconditional pause on the
        ordinary path.  It has **no default in the processor** — it is read
        straight from ``.putio_config`` — so a config that omitted it made
        bash run ``sleep ""`` on every lookup.  The default here is
        ``server_config_template``'s, which is the only place it is written
        down.
    :param on_wait: called with a message when the window is full, so the
        caller can put it in the log the way bash's ``log WARN`` did.
    """

    def __init__(
        self,
        max_requests: int = 35,
        window_seconds: int = 10,
        delay: float = 1.0,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        on_wait: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.delay = delay
        self._clock = clock if clock is not None else time.time
        self._sleep = sleep if sleep is not None else time.sleep
        self._on_wait = on_wait
        self._lock = threading.Lock()
        self._count = 0
        self._window_start = None  # type: Optional[int]

    def acquire(self) -> None:
        """Block until another request may be made, then record it."""
        pace = False

        # Held across the sleep on purpose, exactly as the file lock was: a
        # second caller arriving mid-wait would otherwise read the pre-sleep
        # count, see the window as full too, and wait the same interval in
        # parallel — two waits for one window's worth of relief.
        with self._lock:
            now = int(self._clock())

            if self._window_start is None:
                self._count, self._window_start = 0, now
            elif now - self._window_start >= self.window_seconds:
                self._count, self._window_start = 1, now
            elif self._count >= self.max_requests:
                wait = self.window_seconds - (now - self._window_start) + 1
                if self._on_wait is not None:
                    self._on_wait(
                        "[TMDB] Rate limit reached, waiting {0}s before next request".format(
                            wait
                        )
                    )
                if wait > 0:
                    self._sleep(wait)
                # Re-read the clock after sleeping rather than trusting
                # now+wait, which is what bash did.
                self._count, self._window_start = 1, int(self._clock())
            else:
                self._count += 1
                pace = True

        # Outside the lock: this is the per-request pace, not the window, and
        # holding the lock through it would serialise every caller behind it.
        if pace and self.delay > 0:
            self._sleep(self.delay)
