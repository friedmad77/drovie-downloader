"""The run — phase 5's entry point, and the end of the strangler.

``putio_download_and_delete.sh`` no longer *is* the processor.  It resolves the
binaries, sources ``.putio_config``, answers ``--mount-gate``, and ``exec``s
into ``python3 -m putio run``, which is this module.  Everything between the
pause gate and the summary line lives here.

**Why ``exec`` and not a subprocess.**  ``exec`` replaces the process image and
*keeps the pid*.  That is not a detail — the pid is the run's identity
everywhere outside this package:

* the lockfile holds it, and ``putioctl status`` reports it
* ``putioctl stop`` walks ``pgrep -P`` down from it to find the rclone children
* every progress file is named ``dl.<runpid>.<jobid>``, and readers ``kill -0``
  that number to tell a live transfer from what a killed run left behind

Forking would have made the shim's pid and the worker's pid different, and
every one of those would have needed a story.  Exec'ing means none of them
changed at all.

**What is left in bash, and why it is not a half-migration.**  Three things:

* *PATH and binary discovery.*  Genuinely shell-shaped, and it has to happen
  before there is an interpreter to ask.
* *Sourcing ``.putio_config``.*  The file is a bash script that gets sourced —
  values reference ``$HOME`` and nothing stops one using command substitution.
  A Python reader would be a second implementation of bash's semantics that
  has to agree with the first one forever.  See :mod:`putio.config`.
* *Mount recovery.*  ``osascript``, Finder, ``mount_smbfs``, TCP probes,
  cross-run backoff files and dialog suppression.  The plan's phase 6 says
  this is the one place where stopping is better than finishing, and Python
  would still be shelling out to every bit of it.  It is reached through
  ``--mount-gate``; see :mod:`putio.mount`.

**The interpreter is now load-bearing.**  Through phases 1-4 a missing
``PYTHON_BIN`` cost the features that needed it and the sync carried on, so
every Python call site had a bash fallback behind it.  Those fallbacks are
gone: there is one implementation of each rule now, which is the point of
finishing.  The shim therefore fails loudly at startup when it has no
interpreter rather than silently doing nothing.

**Crash-only, unchanged.**  Nothing here holds state a restart could leave
inconsistent.  The three ``/tmp`` files bash used to pass counters between
subshells (``MOVIES_ADDED_FILE``, ``TV_ADDED_FILE``, ``DOWNLOADS_DONE_FILE``)
are *variables* now — there are no subshells to pass anything between — so
phase 5 removes persistent state rather than adding any.  The put.io delete is
still gated on the local file having left staging, which is what makes put.io
itself the durable work queue.
"""

from __future__ import annotations

import dataclasses
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Sequence, Set

from . import logs, pause, sweep
from .config import Config
from .http import HttpClient, HttpError
from .lock import LockError, RunLock
from .logs import Logger, format_duration
from .mount import ALIVE, MountGate
from .pipeline import Batch, Planner
from .plex import SECTION_TYPES, PlexClient
from .proc import Retrier
from .progress import ProgressPublisher
from .ratelimit import TokenWindow
from .rclone import RcloneClient
from .tmdb import TmdbClient
from .transfer import Downloader

__all__ = ["Run", "build_batch", "main"]

EXIT_OK = 0
EXIT_FAIL = 1

#: The phrase ``putioctl status`` greps for to report the last completed run.
#: **Verbatim, forever.**  It is checked by a test for exactly that reason.
COMPLETION_PHRASE = "Script completed successfully"

#: What bash slept between a Plex scan and poking Drovie, to give the
#: scan a moment to find the new file.
WEBHOOK_DELAY_SECONDS = 10

#: ``RCLONE_STATS_INTERVAL`` must parse as an rclone duration or every download
#: fails at flag-parse time — before ``--log-file`` opens, so with no cause in
#: the log at all.  This knob only drives the bar, so a bad value falls back
#: rather than taking the transfers with it.
_DURATION = re.compile(r"^[0-9]+(ms|s|m|h)?$")


class _Terminated(BaseException):
    """SIGTERM or SIGINT reached the run.

    A ``BaseException`` on purpose: it has to pass straight through the
    ``except Exception`` that keeps one bad file from stopping the queue, or
    ``putioctl stop`` would be swallowed by whichever worker happened to catch
    it.
    """


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
class Wiring(NamedTuple):
    """The batch, plus the two collaborators a caller still needs by name.

    ``rclone`` because the listing and the empty-folder pass are the run's own
    calls, not the batch's, and building a second client would mean a second
    cached size listing.  ``progress`` because a caller abandoning the batch
    has no other way to clear the bars it already published — on the ordinary
    path each job clears its own.
    """

    batch: Batch
    rclone: RcloneClient
    progress: ProgressPublisher


def build_batch(
    config: Config,
    log: Callable[[str, str], None],
    record: Callable[..., None],
    on_download: Callable[[str], None],
) -> Wiring:
    """Everything :class:`~putio.pipeline.Batch` needs, assembled once.

    Shared with the ``download-batch`` subcommand so the hand-run door and the
    run itself cannot be wired differently — which is exactly the drift the
    whole port has been avoiding.
    """
    retrier = Retrier(max_attempts=config.max_retries, delay=config.retry_delay, on_log=log)
    rclone = RcloneClient(config, retrier, log=log)
    progress = ProgressPublisher(config.progress_state_dir, config.run_pid)
    downloader = Downloader(config, rclone, progress, log, on_complete=on_download)
    planner = Planner(
        _batch_tmdb(config, log),
        movie_library=config.movie_library,
        tv_library=config.tv_library,
        log=log,
    )
    mount = MountGate(config, config.script_path, log)
    batch = Batch(config, rclone, downloader, planner, mount, retrier, log, record)
    return Wiring(batch, rclone, progress)


def _batch_tmdb(config: Config, log: Callable[[str, str], None]) -> TmdbClient:
    """A TMDb client with the in-process limiter.

    Phases 2, 3 and 4 all wrote down the same argument for why the rate limiter
    had to stay file-backed: more than one process in a run made TMDb calls.
    Phase 5 is the end of it.  There is one process, its threads share one
    interpreter, and :class:`~putio.ratelimit.TokenWindow` — a fixed window
    behind a lock, no file, no ``flock``, no syscall per lookup — is simply the
    right limiter now.  See :mod:`putio.ratelimit`.
    """
    limiter = TokenWindow(
        max_requests=config.max_requests_per_window,
        window_seconds=config.rate_window_seconds,
        delay=config.rate_limit_delay,
        on_wait=lambda message: log("WARN", message),
    )
    http = HttpClient(
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        on_retry=lambda message: log("WARN", "[RETRY] " + message),
    )
    return TmdbClient(config.tmdb_api_key, http, limiter, on_note=log)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
class Run:
    """One cron tick, from the pause gate to the summary line."""

    def __init__(self, config: Config, log: Optional[Logger] = None) -> None:
        self.config = config
        self.log = log if log is not None else Logger(config.log_file, config.log_level)

        self.started = int(time.time())
        self.completed = False

        # What used to live in /tmp because bash's download jobs were subshells
        # and a variable set in one was invisible to the parent.  There are no
        # subshells; these are variables.  The lock is real: `ADDED` and the
        # download tally are both written from pool threads.
        self._results_lock = threading.Lock()
        self.added: Set[str] = set()
        self.downloaded = 0
        self.local_existing = 0
        self.putio_file_count = 0
        self.plex_scanned = ""

        self._lock = RunLock(config.lockfile, self.log)
        self._progress: Optional[ProgressPublisher] = None

    # ------------------------------------------------------------------
    # the record channel, now that both ends are in this process
    # ------------------------------------------------------------------
    def _record(self, kind: str, *fields: str) -> None:
        if kind == "ADDED" and fields:
            with self._results_lock:
                self.added.add(fields[0])

    def _note_download(self, _name: str) -> None:
        with self._results_lock:
            self.downloaded += 1

    # ------------------------------------------------------------------
    # validate_config
    # ------------------------------------------------------------------
    def validate(self) -> List[str]:
        """``validate_config`` — the errors it would have refused to start on.

        Bash ``echo``ed these to stdout and nothing else, so a run that refused
        to start over a missing key left **nothing in the log** saying why —
        only the EXIT trap's "Exited without completing".  They are logged at
        ERROR here as well as printed.  That is additive (nothing greps these
        lines) and it closes a real gap; the printing is kept because cron
        mails stdout and that is how the operator finds out at all.
        """
        config = self.config
        errors: List[str] = []

        if os.environ.get("CRON_DEBUG") or not os.environ.get("TERM"):
            self.log("INFO", "[ENV] Running in cron/non-interactive mode")
            self.log("INFO", "[ENV] HOME=" + os.environ.get("HOME", ""))
            self.log("INFO", "[ENV] PATH=" + os.environ.get("PATH", ""))
            self.log("INFO", "[ENV] RCLONE_CONFIG=" + os.environ.get("RCLONE_CONFIG", ""))
            self.log("INFO", "[ENV] RCLONE_CMD=" + config.rclone_cmd)
            self.log("INFO", "[ENV] JQ_CMD=" + config.jq_cmd)
            self.log("INFO", "[ENV] PYTHON_BIN=" + (config.python_bin or "<none>"))
            self.log("INFO", "[ENV] CONFIG_FILE=" + config.config_file)

        if not _DURATION.match(config.rclone_stats_interval or ""):
            self.log(
                "WARN",
                "[CONFIG] RCLONE_STATS_INTERVAL='{0}' is not a duration — using 5s".format(
                    config.rclone_stats_interval
                ),
            )
            self.config = config = _replace_interval(config, "5s")

        if not config.tmdb_api_key:
            errors.append("ERROR: TMDB_API_KEY not set in configuration")
        if not config.plex_token:
            errors.append("ERROR: PLEX_TOKEN not set in configuration")

        errors.extend(self._validate_mount())

        # When staging is on a separate (often USB) volume, fail loud at
        # startup if it is not writable — a USB drive cannot be remounted
        # programmatically, so there is no point limping into the run.
        if config.use_temp_dir:
            root = config.staging_dir or os.path.expanduser("~/.putio_staging")
            try:
                os.makedirs(root, exist_ok=True)
            except OSError:
                errors.append("ERROR: STAGING_DIR is not writable: " + root)
            else:
                if not os.access(root, os.W_OK):
                    errors.append("ERROR: STAGING_DIR is not writable: " + root)

        if not os.path.isdir(config.local_dir):
            errors.append("ERROR: LOCAL_DIR does not exist: " + config.local_dir)

        return errors

    def _validate_mount(self) -> List[str]:
        """The preflight, with bash's "in the mount table? then proceed" rule.

        If the mount table still shows the share, a failing health check is not
        grounds to refuse the run — downstream I/O will surface anything real,
        and the per-file gate still runs.  Only a share genuinely gone from the
        table, and unrecoverable, aborts.
        """
        config = self.config
        if not config.media_mount_point:
            return []

        gate = MountGate(config, config.script_path, self.log)
        if gate.probe() == ALIVE:
            return []

        if _in_mount_table(config.media_mount_point):
            self.log(
                "WARN",
                "[MOUNT] Preflight stat failed but {0} is in mount table — proceeding".format(
                    config.media_mount_point
                ),
            )
            return []
        return [
            "ERROR: media mount {0} is not available and could not be remounted".format(
                config.media_mount_point
            )
        ]

    # ------------------------------------------------------------------
    # the body
    # ------------------------------------------------------------------
    def execute(self) -> int:
        errors = self.validate()

        # *After* validate(), never before. validate() is the one place the
        # processor overrides a configured value — a malformed
        # RCLONE_STATS_INTERVAL is replaced with 5s — and Config is frozen, so
        # it does that by rebinding self.config to a new instance. Reading it
        # into a local first left this method (and every collaborator
        # build_batch hands it to) holding the *old* object, which made the
        # fallback a no-op: the warning was logged and rclone was still invoked
        # with `--stats=5sec`, which it rejects at flag-parse time, before
        # --log-file opens, so every transfer in the run failed with no cause
        # in the log. That is exactly the failure the fallback exists to
        # prevent.
        config = self.config

        if errors:
            for line in errors:
                print(line)
                self.log("ERROR", line)
            print("Configuration validation failed. Please check your settings.")
            return EXIT_FAIL

        try:
            os.makedirs(config.unmatched_dir, exist_ok=True)
        except OSError as exc:
            self.log("ERROR", "[CONFIG] Could not create {0}: {1}".format(
                config.unmatched_dir, exc))

        logs.rotate(config.log_file)

        # DEBUG on purpose: cron fires every few minutes and most runs find
        # nothing to do, so a run that did no work costs exactly one line.  The
        # single completion line at the bottom carries the outcome, and
        # LOG_LEVEL=DEBUG brings all of this back.
        self.log("DEBUG", "=== Starting put.io download script ===")
        self.log("DEBUG", "[CONFIG] Local dir: " + config.local_dir)
        self.log("DEBUG", "[CONFIG] Min free space: {0}GB".format(config.min_free_space_gb))
        self.log("DEBUG", "[CONFIG] Max retries: {0}".format(config.max_retries))

        sweep.sweep_markers(config.local_dir, self.log)
        sweep.sweep_progress(config.progress_state_dir)
        sweep.sweep_staging(
            config.staging_dir or os.path.expanduser("~/.putio_staging"), self.log
        )
        sweep.sweep_tombstones(config.local_dir, self.log)

        wiring = build_batch(config, self.log, self._record, self._note_download)
        batch, rclone = wiring.batch, wiring.rclone
        self._progress = wiring.progress

        # The listing, taken before the local sweep because both are inputs to
        # the same pass.  It is a read against put.io and nothing local can
        # change it.
        listing = rclone.list_files()
        if listing is None:
            self.log("ERROR", "[PUTIO] Failed to list files from put.io")
            self.log("ERROR", "[PUTIO] rclone command: " + config.rclone_cmd)
            self.log("ERROR", "[PUTIO] PATH: " + os.environ.get("PATH", ""))
            return EXIT_FAIL

        self.putio_file_count = len(listing)
        if listing:
            self.log("INFO", "[PUTIO] Found {0} files".format(len(listing)))
            if len(listing) <= 10:
                self.log("INFO", "[PUTIO] File list:")
                for name in listing:
                    self.log("INFO", "[PUTIO]   " + name)
        else:
            self.log("DEBUG", "[PUTIO] No files found")

        # There is no fallback to fall back *to* any more, and the START/DONE
        # contract went with it: it existed so bash could tell "the batch
        # committed and died" from "nothing happened", and bash is no longer
        # holding a stale listing to re-run.  What the contract protected still
        # holds, and now trivially — a run that dies mid-batch is simply a run
        # that died, and the next cron tick re-derives its work from the world.
        self.local_existing = batch.sweep_local()
        batch.run(listing)

        self.scan_plex(rclone)
        self.remove_empty_folders(rclone)
        self.summarise()
        return EXIT_OK

    # ------------------------------------------------------------------
    # Plex
    # ------------------------------------------------------------------
    def scan_plex(self, _rclone) -> None:
        """Refresh whichever libraries got something, then poke Drovie.

        A transcription of ``scan_plex_library`` called once for both
        libraries.  The webhook still fires **once per successful scan** with
        its own ten-second wait, as bash does when a film and an episode landed
        in the same run.  Collapsing that to one call would be better and is
        still deliberately not this phase's change.
        """
        config = self.config
        wanted = []
        if "movie" in self.added:
            self.log("INFO", "[PLEX] Movies were added, triggering scan...")
            wanted.append(("movie", config.plex_movie_library_id))
            self.plex_scanned = "movies"
        else:
            self.log("DEBUG", "[PLEX] No movies added, skipping scan")

        if "tv" in self.added:
            self.log("INFO", "[PLEX] TV shows were added, triggering scan...")
            wanted.append(("tv", config.plex_tv_library_id))
            self.plex_scanned = (
                self.plex_scanned + " and TV" if self.plex_scanned else "TV"
            )
        else:
            self.log("DEBUG", "[PLEX] No TV shows added, skipping scan")

        if not wanted:
            return

        if not config.plex_token:
            # validate() already refused to start without one, so this is
            # unreachable in practice; bash said it and so does this.
            self.log("WARN", "[PLEX] PLEX_TOKEN not set, skipping scan")
            return

        http = HttpClient(
            max_retries=config.max_retries,
            retry_delay=config.retry_delay,
            on_retry=lambda message: self.log("WARN", "[RETRY] " + message),
        )
        plex = PlexClient(config.plex_url, config.plex_token, http)

        for library_type, library_id in wanted:
            section_type = SECTION_TYPES[library_type]
            if not library_id:
                self.log("INFO", "[PLEX] Detecting library ID for {0}...".format(library_type))
                try:
                    library_id = plex.library_id(section_type) or ""
                except HttpError as exc:
                    # New since phase 2: `curl -s` could not tell a 401 from an
                    # empty library list, so both arrived as "could not find".
                    self.log("ERROR", "[PLEX] Sections lookup failed: {0}".format(exc))
                    library_id = ""
                if not library_id:
                    self.log(
                        "WARN",
                        "[PLEX] Could not find library ID for type '{0}'".format(section_type),
                    )
                    self.log(
                        "ERROR",
                        "[PLEX] Could not determine library ID for {0}".format(library_type),
                    )
                    continue

            self.log(
                "INFO",
                "[PLEX] Triggering scan for {0} library (ID: {1})...".format(
                    library_type, library_id
                ),
            )
            try:
                plex.refresh(library_id)
            except HttpError as exc:
                self.log("ERROR", "[PLEX] Failed to trigger {0} library scan".format(library_type))
                self.log("ERROR", "[PLEX] {0}".format(exc))
                continue
            self.log(
                "INFO", "[PLEX] {0} library scan triggered successfully".format(library_type)
            )
            self._fire_webhook(plex)

    def _fire_webhook(self, plex: PlexClient) -> None:
        config = self.config
        if not (config.drovie_webhook_url and config.drovie_webhook_key):
            return
        delay = _webhook_delay()
        self.log(
            "INFO",
            "[WEBHOOK] Waiting {0} seconds before triggering Drovie "
            "availability check...".format(delay),
        )
        if delay > 0:
            time.sleep(delay)
        try:
            plex.trigger_webhook(
                config.drovie_webhook_url, config.drovie_webhook_key
            )
        except HttpError as exc:
            self.log("WARN", "[WEBHOOK] Failed to trigger Drovie availability check")
            self.log("WARN", "[WEBHOOK] {0}".format(exc))
            return
        self.log("INFO", "[WEBHOOK] Drovie availability check triggered successfully")

    # ------------------------------------------------------------------
    # empty folders
    # ------------------------------------------------------------------
    def remove_empty_folders(self, rclone) -> None:
        """Delete directories put.io is left holding with nothing in them."""
        self.log("DEBUG", "[PUTIO] Checking for empty folders...")
        directories = rclone.list_dirs()
        if not directories:
            return
        for directory in directories:
            contents = rclone.list_dir(directory)
            # None is "rclone failed", which is not the same as empty — deleting
            # on a failed listing is how a folder with files in it gets removed.
            if contents is None or contents:
                continue
            if rclone.rmdir(directory, "delete empty folder " + directory):
                self.log("INFO", "[PUTIO] Deleted empty folder: " + directory)
            else:
                self.log("ERROR", "[PUTIO] Failed to delete empty folder: " + directory)

    # ------------------------------------------------------------------
    # the one line a run costs
    # ------------------------------------------------------------------
    def summarise(self) -> str:
        """The completion line, which carries the whole outcome of the run.

        ``Script completed successfully`` is **verbatim** because
        ``putioctl status`` greps for it.

        The count is what *landed*, not what the listing offered: a run whose
        share dropped mid-dispatch, or whose transfers all failed their
        retries, must not report the queue as though it had done it.  When
        put.io had files and none arrived, say so rather than falling through
        to "nothing to do" — that combination is the one worth noticing.
        """
        parts: List[str] = []
        if self.downloaded > 0:
            parts.append("{0} downloaded".format(self.downloaded))
        elif self.putio_file_count > 0:
            parts.append(
                "nothing downloaded ({0} file(s) on put.io)".format(self.putio_file_count)
            )
        if self.local_existing > 0:
            parts.append("{0} local file(s)".format(self.local_existing))
        if self.plex_scanned:
            parts.append("Plex scan: " + self.plex_scanned)

        summary = ", ".join(parts) or "nothing to do"
        elapsed = format_duration(int(time.time()) - self.started)
        line = "=== {0} — {1} ({2}) ===".format(COMPLETION_PHRASE, summary, elapsed)
        self.completed = True
        self.log("INFO", line)
        return line

    # ------------------------------------------------------------------
    # cleanup — what the EXIT trap did
    # ------------------------------------------------------------------
    def cleanup(self, status: int) -> None:
        """Release the lock and clear this run's bars.

        None of it is required for correctness — that is what crash-only means,
        and the startup sweeps are the proof: every one of them exists because
        a killed run never gets here.  It is here so the ordinary case leaves
        nothing for the next run to tidy.
        """
        if self._progress is not None:
            self._progress.clear_all()
        self._lock.release()
        # The start banner is DEBUG, so without this a run that died partway
        # would leave no INFO trace at all and look indistinguishable from cron
        # never firing.
        if not self.completed:
            self.log("WARN", "[RUN] Exited without completing (status {0})".format(status))


def _replace_interval(config: Config, value: str) -> Config:
    """A copy of the config with a corrected stats interval.

    :class:`Config` is frozen, which is what stops a component quietly editing
    the run's settings out from under another one.  This is the single place
    the processor itself overrides a configured value, and it does it by
    building a new config rather than by making the type mutable.
    """
    return dataclasses.replace(config, rclone_stats_interval=value)


def _webhook_delay() -> int:
    """Overridable only so the harness does not have to wait it out."""
    raw = os.environ.get("PUTIO_WEBHOOK_DELAY", "")
    if raw.isdigit():
        return int(raw)
    return WEBHOOK_DELAY_SECONDS


def _in_mount_table(mount_point: str) -> bool:
    """``mount | grep -q " on $MEDIA_MOUNT_POINT "``."""
    try:
        result = subprocess.run(
            ["/sbin/mount"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    needle = " on {0} ".format(mount_point)
    return needle in result.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def _holder_elapsed(pid: Optional[int]) -> str:
    """``, elapsed 02:14:31``, or nothing at all if ``ps`` cannot say.

    The same ``ps -o etime=`` ``putioctl sync`` prints when it refuses for the
    same reason, so the two agree on the wording.  One subprocess on a path
    that is taken at most once per cron tick and only while a run is already
    holding the lock.
    """
    if pid is None:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    elapsed = result.stdout.decode("ascii", "replace").strip()
    return ", elapsed {0}".format(elapsed) if elapsed else ""


def _install_signal_handlers() -> None:
    """Turn ``putioctl stop``'s SIGTERM into an exception the ``finally`` sees.

    Bash's EXIT trap did not reliably fire on SIGTERM, so a stopped run could
    leave its lockfile and its bars behind for the next run's sweeps to find.
    Handling it here means the ordinary stop is clean — while the sweeps stay,
    because SIGKILL still exists and so does the power cable.
    """

    def handler(signum, _frame):
        raise _Terminated("signal {0}".format(signum))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m putio run`` — one cron tick."""
    config = Config.from_env()
    logger = Logger(config.log_file, config.log_level)

    # Before the lock, so a paused tick leaves no lockfile and no trace.  Exit
    # 0 on purpose: cron mails failures, and a run skipped deliberately is not
    # one.
    if pause.check_pause(config.pause_file, config.log_file, logger):
        return EXIT_OK

    run = Run(config, logger)
    try:
        run._lock.acquire()
    except LockError as exc:
        # Not an error, and not a failure.  cron fires every few minutes while
        # a queue of multi-gigabyte transfers runs for hours, so a tick that
        # finds the lock held is the *designed* behaviour of the schedule — the
        # work is already happening in the run that holds it.  Bash logged this
        # at ERROR and exited non-zero, which put a red line in `putioctl logs
        # --errors` for every five minutes of a long download and told an exit-
        # status watcher a run had failed.  Same reasoning as the pause gate
        # above: a tick skipped deliberately is not a failed one.
        #
        # What is worth seeing is *how long* the holder has been going, since
        # that is the only thing that separates an ordinary overlap from a run
        # wedged on something — so the line carries it instead of a level.
        if exc.holder is None:
            # The other branch: a stale lock was broken and another tick took
            # it in the gap.  Rare, self-resolving, and still not a failed
            # sync — but unlike an ordinary overlap it is worth noticing.
            logger("WARN", str(exc))
            return EXIT_OK
        logger(
            "INFO",
            "[LOCK] A sync is already running (PID {0}{1}) — skipping this "
            "run".format(exc.holder, _holder_elapsed(exc.holder)),
        )
        return EXIT_OK

    _install_signal_handlers()

    status = EXIT_OK
    try:
        status = run.execute()
    except _Terminated as exc:
        logger("WARN", "[RUN] Stopped: {0}".format(exc))
        status = 143
    except Exception as exc:  # noqa: BLE001 - the run must still release its lock
        logger("ERROR", "[RUN] Unhandled error: {0}: {1}".format(type(exc).__name__, exc))
        status = EXIT_FAIL
    finally:
        run.cleanup(status)
    return status


if __name__ == "__main__":  # pragma: no cover - `python3 -m putio run` is the door
    sys.exit(main())
