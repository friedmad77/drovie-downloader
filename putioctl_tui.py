#!/usr/bin/env python3
"""Interactive console for the put.io -> Plex sync.

Streams the processor log in the upper pane and takes commands on a prompt at
the bottom, so watching a transfer and acting on it are the same session.

This is the presentation layer only: every command is executed by handing it to
the `putioctl` shell CLI, and the paths it works with are resolved by that same
CLI and passed in on the command line. Nothing about config discovery, pausing
or locking is reimplemented here, so the TUI cannot drift from what cron does.

Standard library only (curses), so it runs on a stock macOS python3.
"""

import argparse
import curses
import json
import locale
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

MAX_BUFFER_LINES = 5000      # log lines kept in memory for scrollback
POLL_INTERVAL_MS = 100       # UI tick; also how often the log file is polled
BLINK_SECONDS = 0.53         # half-period of the cursor blink, as terminals use
SLOW_REFRESH_SECONDS = 15    # mount/disk probes, which can block on a dead NAS
PROGRESS_REFRESH_SECONDS = 2  # transfer probes; the processor ticks every 5s
MAX_TRANSFER_ROWS = 3        # bars drawn above the status bar
MIN_HEIGHT_FOR_TRANSFERS = 14  # below this the log pane needs every row it has
MILESTONE_STEP = 10          # percent between the bars kept in the scrollback

BAR_FILL, BAR_EMPTY = "█", "░"

# Styles, resolved to curses colour pairs at startup.
S_NORMAL, S_DIM, S_ERROR, S_WARN, S_TAG, S_CMD, S_OUT, S_BAR, S_ACCENT, S_BAR_ALERT = range(10)

LEVEL_RE = re.compile(r"^\[[\d\-: ]+\]\s+\[(\w+)\]")
TAG_RE = re.compile(r"\[(PAUSE|MOUNT|PLEX|PUTIO|PARALLEL|LOCK|LOCAL|CONFIG)\]")
# The single line an idle cron run leaves behind — see `putioctl quiet`. The
# em dash the processor writes is spanned by `.*` rather than matched, so this
# does not depend on how the log's encoding survives the round trip.
IDLE_RE = re.compile(r"\[INFO\] === Script completed successfully .* nothing to do \(")

COMMANDS = [
    "status", "sync", "pause", "resume", "stop", "progress",
    "config", "logs", "quiet", "help", "clear", "quit", "exit",
]
CONFIG_SUBCOMMANDS = ["list", "get", "set", "unset", "edit", "path"]

# Every buffered line is one of these. K_LOG is the processor's own log; K_UI is
# everything the console itself put on screen — the banner, command echoes and
# command output — which `clear` can sweep away to leave a pure tail. K_IDLE is
# log too, split out so `quiet` can hide those lines at render time: they stay in
# the buffer either way, which is what lets the toggle work in both directions
# on scrollback that has already streamed past.
# K_XFER is a bar the journal left behind: log-like rather than chatter, so
# `clear` keeps it and `quiet` never hides it.
K_LOG, K_UI, K_IDLE, K_XFER = "log", "ui", "idle", "xfer"

# Commands that need the real terminal to themselves. Their output can't be
# piped into the pane: an editor handed a pipe warns that output is not a
# terminal, spills the file into the log and then fights the console for every
# keystroke. These get the screen instead — see Console.run_foreground.
INTERACTIVE = {("config", "edit")}


# ---------------------------------------------------------------------------
# Log following
# ---------------------------------------------------------------------------

class LogTail:
    """Follows a log file across rotations.

    The processor rewrites its log at the start of every run (it keeps the last
    24 hours), replacing the file rather than appending — so a plain read loop
    on one file handle goes deaf after the first rotation. Both signals of that
    are checked here: a changed inode, and a file that shrank below our offset.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None
        self._inode = None
        self._partial = ""
        self._last_line = ""         # last complete line handed out

    def prime(self, max_lines):
        """Returns the tail of the existing log, then follows from its end."""
        lines = []
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "r", errors="replace") as fh:
                fh.seek(max(0, size - 64 * 1024))
                if size > 64 * 1024:
                    fh.readline()          # discard a partial first line
                lines = fh.read().splitlines()[-max_lines:]
            if lines:
                self._last_line = lines[-1]
        except OSError:
            pass
        self._open(seek_end=True)
        return lines

    def _open(self, seek_end=False):
        try:
            fh = open(self.path, "r", errors="replace")
        except OSError:
            self._fh = None
            return
        if seek_end:
            fh.seek(0, os.SEEK_END)
        self._fh = fh
        try:
            self._inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            self._inode = None

    def read_new(self):
        """Returns any complete lines written since the last call."""
        if self._fh is None:
            self._open()
            if self._fh is None:
                return []

        rotated = False
        try:
            stat = os.stat(self.path)
            if stat.st_ino != self._inode or stat.st_size < self._fh.tell():
                self._fh.close()
                self._open()                # rotated or truncated: start over
                rotated = True
                self._partial = ""          # a fragment of the old file is junk
                if self._fh is None:
                    return []
        except OSError:
            return []

        try:
            chunk = self._fh.read()
        except OSError:
            return []
        if not chunk:
            return []

        chunk = self._partial + chunk
        self._partial = ""
        if not chunk.endswith("\n"):
            chunk, _, self._partial = chunk.rpartition("\n")
        lines = chunk.splitlines()
        if rotated:
            lines = self._drop_seen(lines)
        if lines:
            self._last_line = lines[-1]
        return lines

    def _drop_seen(self, lines):
        """Skip what the reopened file repeats.

        Rotation replaces the log with a filtered copy of itself, so reading the
        new file from the top hands back the whole retained history — which the
        pane has already shown, and which then appears twice. The last line we
        gave out is the seam: everything up to and including it is scrollback.
        Searched from the end, since a rotated log holds one copy of it and
        untimestamped rclone lines can repeat verbatim earlier on.

        A last line that isn't there at all means this is not the file we were
        reading (a log deleted and started fresh), and all of it is new.
        """
        if not self._last_line:
            return lines
        for i in range(len(lines) - 1, -1, -1):
            if lines[i] == self._last_line:
                return lines[i + 1:]
        return lines


# ---------------------------------------------------------------------------
# Live state for the status bar
# ---------------------------------------------------------------------------

class StateProbe:
    """Cheap, non-blocking reads of the same state files the CLI uses.

    Anything that touches the media mount (its mount status, free space) is
    deliberately kept off the UI thread: a stat on a dropped SMB share blocks
    in the kernel, and that would freeze the console exactly when the log is
    most worth reading.
    """

    def __init__(self, paths):
        self.paths = paths
        self.mount_ok = None
        self.free_space = None
        self.transfers = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._slow_loop, daemon=True)
        self._thread.start()
        # Transfers get their own loop: they want a couple of seconds, the
        # mount probes want fifteen, and sharing one loop would mean either
        # bars that lag the log or a `mount` call every two seconds.
        self._progress_thread = threading.Thread(target=self._progress_loop, daemon=True)
        self._progress_thread.start()

    def stop(self):
        self._stop.set()

    # -- fast probes, safe on the UI thread --------------------------------

    def running_pid(self):
        try:
            with open(self.paths.lockfile) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def pause_state(self):
        """(paused, until_epoch_or_None, reason) — until None means indefinite."""
        try:
            with open(self.paths.pause_file) as fh:
                text = fh.read()
        except OSError:
            return (False, None, "")
        until, reason = None, ""
        for line in text.splitlines():
            if line.startswith("PAUSED_UNTIL="):
                raw = line.split("=", 1)[1].strip().strip('"')
                until = int(raw) if raw.isdigit() and int(raw) > 0 else None
            elif line.startswith("PAUSED_REASON="):
                reason = line.split("=", 1)[1].strip().strip('"')
        if until is not None and time.time() >= until:
            return (False, None, "")        # expired; next run clears the file
        return (True, until, reason)

    # -- slow probes, background thread ------------------------------------

    def _slow_loop(self):
        while not self._stop.is_set():
            self._refresh_mount()
            self._refresh_space()
            self._stop.wait(SLOW_REFRESH_SECONDS)

    def _refresh_mount(self):
        if not self.paths.mount_point:
            return
        try:
            # `mount` reads the kernel's table and never stats the share, so it
            # returns promptly even when the NAS is gone.
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                 timeout=10).stdout
            self.mount_ok = f" on {self.paths.mount_point} " in out
        except (OSError, subprocess.SubprocessError):
            self.mount_ok = None

    def _progress_loop(self):
        while not self._stop.is_set():
            self._refresh_transfers()
            self._stop.wait(PROGRESS_REFRESH_SECONDS)

    def _refresh_transfers(self):
        """Read the processor's per-download progress files.

        On this thread rather than the UI one for the same reason as the mount
        probes: during the staged copy the only measure of progress is the size
        of the file appearing on the share, and that stat blocks in the kernel
        if the share has dropped.
        """
        rows = []
        directory = self.paths.progress_dir
        if directory:
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                names = []
            for name in names:
                # dl.<runpid>.<jobid>.meta — a run killed mid-transfer never
                # cleans up, so its leftovers are skipped rather than drawn as
                # a bar that will never move again.
                if not (name.startswith("dl.") and name.endswith(".meta")):
                    continue
                parts = name.split(".")
                if len(parts) < 4 or not parts[1].isdigit():
                    continue
                try:
                    os.kill(int(parts[1]), 0)
                except OSError:
                    continue
                row = self._read_transfer(os.path.join(directory, name))
                if row:
                    rows.append(row)
        # Rebound whole: the UI thread only ever sees a complete list.
        self.transfers = rows

    def _read_transfer(self, meta_path):
        meta = {}
        try:
            with open(meta_path) as fh:
                for line in fh:
                    key, _, value = line.rstrip("\n").partition("=")
                    meta[key] = value
        except OSError:
            return None
        name = meta.get("NAME", "")
        if not name:
            return None
        try:
            size = int(meta.get("SIZE", "0"))
        except ValueError:
            size = 0
        # `key` is the meta filename: dl.<runpid>.<jobid>, which is the only
        # thing that identifies one transfer across probes. Two releases can
        # share a NAME, and a job that ends is replaced by a different jobid.
        row = {"key": os.path.basename(meta_path), "name": name, "size": size,
               "phase": meta.get("PHASE", ""),
               "bytes": None, "speed": None, "eta": None}
        if row["phase"] == "copy":
            try:
                row["bytes"] = os.path.getsize(meta.get("DEST", ""))
            except OSError:
                pass
            return row
        stats = self._last_stats(meta.get("LOG", ""))
        if stats:
            row["bytes"] = stats.get("bytes")
            row["speed"] = stats.get("speed")
            row["eta"] = stats.get("eta")
        return row

    @staticmethod
    def _last_stats(log_path):
        """The stats object from the last line rclone appended to its JSON log.

        Only the tail is read: the file grows for the life of the transfer, and
        everything before the last few lines is history nobody is drawing.
        """
        if not log_path:
            return None
        try:
            with open(log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 16384))
                chunk = fh.read().decode("utf-8", "replace")
        except OSError:
            return None
        for line in reversed(chunk.splitlines()):
            if '"stats"' not in line:
                continue
            try:
                return json.loads(line).get("stats")
            except ValueError:
                continue        # a partial first line, or a truncated last one
        return None

    def _refresh_space(self):
        if not self.paths.local_dir:
            return
        try:
            out = subprocess.run(["df", "-h", self.paths.local_dir],
                                 capture_output=True, text=True, timeout=10).stdout
            rows = out.splitlines()
            self.free_space = rows[1].split()[3] if len(rows) > 1 else None
        except (OSError, subprocess.SubprocessError, IndexError):
            self.free_space = None


def next_cron_run(schedule):
    """Next fire time for a `*/N * * * *` schedule, else None.

    Only the every-N-minutes form is predicted: guessing at a general cron
    expression would be worse than showing nothing.
    """
    if not schedule:
        return None
    m = re.match(r"^\*/(\d+) \* \* \* \*$", schedule.strip())
    if not m:
        return None
    step = int(m.group(1))
    if step <= 0:
        return None
    now = time.localtime()
    # `*/N` is the set {0, N, 2N, ...} below 60, restarted every hour. For an N
    # that doesn't divide 60 the last slot of the hour is short, so the next
    # fire is :00 of the next hour rather than the current minute plus N.
    candidate = (now.tm_min // step + 1) * step
    if candidate >= 60:
        candidate = 60
    ahead = (candidate - now.tm_min) * 60 - now.tm_sec
    return time.time() + ahead


def human_delta(seconds, precise=False):
    """Rounded by default; `precise` keeps seconds below the hour.

    The bar redraws ten times a second, so the next-run countdown wants
    seconds — rounded to whole minutes it looks frozen. A pause measured in
    hours does not, hence the flag rather than a blanket change. Seconds are
    zero-padded so the surrounding bits don't shift as the number ticks down.
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s" if precise else f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def human_bytes(value):
    if value is None:
        return "?"
    size, units, idx = float(value), ("B", "KiB", "MiB", "GiB", "TiB"), 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.0f} {units[idx]}" if idx == 0 else f"{size:.1f} {units[idx]}"


def transfer_pct(row):
    """A transfer's completion, or None while there is nothing to divide by.

    A size of zero is "not known yet" rather than "an empty file": the meta is
    published before the first byte moves, and `rclone lsjson` may not have
    answered when it was written.
    """
    size, done = row["size"], row["bytes"]
    if size > 0 and done is not None:
        return max(0, min(100, int(done * 100 / size)))
    return None


def transfer_segments(row, width, note=""):
    """One transfer as [(text, style)], fitted to `width`.

    The line gives up detail in stages as the terminal narrows — speed and ETA
    go first, then the byte counts, then the bar — but never the name or the
    percentage. Those two are the answer to "is this thing moving, and what is
    it?", which is the whole reason the strip is on screen.

    `note` rides on every rung of that ladder rather than being appended once,
    so it survives a narrow terminal: it is what the journal's closing line
    says, and a bar that reads 43% with no word about why it stopped there is
    worse than no line at all.
    """
    size, done = row["size"], row["bytes"]
    pct = transfer_pct(row)

    if pct is None:
        tails = ["copying…"] if row["phase"] == "copy" else ["starting…"]
    elif row["phase"] == "copy":
        tails = [f"{pct:3d}%  {human_bytes(done)} / {human_bytes(size)} · copying to the library",
                 f"{pct:3d}%  {human_bytes(done)} / {human_bytes(size)} · copying",
                 f"{pct:3d}%  copying",
                 f"{pct:3d}%"]
    else:
        head = f"{pct:3d}%  {human_bytes(done)} / {human_bytes(size)}"
        extra = ""
        if row.get("speed"):
            extra += f" · {human_bytes(row['speed'])}/s"
        if row.get("eta") is not None and row["eta"] >= 0:
            extra += f" · ETA {human_delta(row['eta'], precise=True)}"
        tails = [head + extra, head, f"{pct:3d}%"]

    if note:
        tails = [tail + " · " + note for tail in tails]

    # A fixed name column rather than one sized to this row's numbers: with two
    # transfers in flight the bars line up under each other, and a bar starting
    # in a different place on every row is hard to read at a glance.
    bar_width = 14
    name_width = max(12, min(34, width - bar_width - 44))
    name = row["name"]
    if len(name) > name_width:
        name = name[:name_width - 1] + "…"

    segments = [("  ⇣ ", S_ACCENT), (name.ljust(name_width) + "  ", S_NORMAL)]
    used = sum(len(text) for text, _ in segments)
    if used + bar_width + 2 + len(tails[-1]) + 1 <= width:
        filled = 0 if pct is None else pct * bar_width // 100
        segments.append((BAR_FILL * filled + BAR_EMPTY * (bar_width - filled) + "  ", S_TAG))
        used += bar_width + 2
    for tail in tails:
        if used + len(tail) + 1 <= width:
            segments.append((tail, S_DIM))
            break
    return segments


class TransferJournal:
    """Turns the live transfer strip into lines the scrollback keeps.

    The strip above the status bar is a *view*: it redraws in place, and a
    transfer that ends leaves nothing behind — so scrolling back through a
    night's downloads found the processor's own [DOWNLOAD] lines and no sense
    of how any of them went. This emits the same bar into the log pane at
    milestones instead: first sight, every MILESTONE_STEP percent, each change
    of phase, and once more when the transfer leaves the strip. That is a
    dozen-odd lines per download rather than the ~900 a line per probe would
    cost, which matters because the pane keeps MAX_BUFFER_LINES and the
    processor's own lines are the ones that would be pushed out.

    Percentages are compared as buckets rather than outright, so a bar that
    stalls and resumes emits nothing extra; the bucket resets on a phase change
    because the staged copy onto the share starts its own count from zero.
    """

    def __init__(self, step=MILESTONE_STEP):
        self.step = max(1, step)
        self._seen = {}       # key -> (phase, bucket, last row seen)
        self._last = None     # the list last examined, to skip no-op ticks

    def _bucket(self, row):
        pct = transfer_pct(row)
        return -1 if pct is None else pct // self.step

    @staticmethod
    def _crossed(bucket, previous):
        """Whether a change of bucket is worth a line.

        Upwards is a milestone. **Downwards is a restart** and matters just as
        much: `Retrier` re-runs rclone with the same argv and the same
        `--log-file`, and a fresh rclone process counts its stats from zero —
        so a 14 GiB download that dies at 85% and retries would otherwise emit
        nothing at all until it climbed back past where the first attempt
        stopped. Hours of scrollback, in which the one thing that happened is
        the one thing not recorded. Bytes are monotonic within a single rclone
        process, so a whole bucket lost is a real restart rather than jitter.

        A bucket of -1 is "no percentage yet", which on the way *down* means
        the reader lost sight of the file rather than the transfer starting
        over: a dropped share makes the copy phase's `getsize` fail and
        `bytes` go None. Treating that as a restart would put a line in the
        log every couple of seconds for as long as the share stayed away.
        """
        if bucket > previous:
            return True
        return 0 <= bucket < previous

    def update(self, transfers):
        """Returns [(row, note)] to append to the scrollback, oldest first.

        `transfers` is rebound whole by the probe thread, so identity is enough
        to skip the ticks between its two-second refreshes — the UI loop runs
        ten times a second and most of those have nothing new to look at.
        """
        if transfers is self._last:
            return []
        self._last = transfers

        out, live = [], set()
        for row in transfers:
            key = row.get("key")
            if not key:
                continue
            live.add(key)
            bucket = self._bucket(row)
            prev = self._seen.get(key)
            if (prev is None or prev[0] != row["phase"]
                    or self._crossed(bucket, prev[1])):
                out.append((row, ""))
                self._seen[key] = (row["phase"], bucket, row)
            else:
                # Still remember the row: it is what the closing line renders.
                self._seen[key] = (prev[0], prev[1], row)

        for key in [k for k in self._seen if k not in live]:
            # The last sample before a job cleared its files, which for a
            # transfer that finished is within one probe of 100% and for one
            # that was killed or failed is exactly where it stopped. The
            # console cannot tell those apart — the processor's next log line
            # does — so the note says only that the bar is over.
            out.append((self._seen.pop(key)[2], "ended"))
        return out


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

class CommandRunner:
    """Runs putioctl subcommands off the UI thread, streaming their output."""

    def __init__(self, paths, sink):
        self.paths = paths
        self.sink = sink
        self.busy = threading.Event()

    def run(self, argv):
        """Starts the command; returns False if the slot was busy or it failed
        to start, so a caller with local state to match can hold it back."""
        if self.busy.is_set():
            self.sink(("A command is still running — wait for it to finish.", S_WARN))
            return False
        # Claim the slot here, on the UI thread that also does the checking.
        # Setting it inside the worker leaves a window in which a second
        # submission passes the check before the first thread has started — two
        # `config set` runs racing that way lose an edit and clobber the backup.
        self.busy.set()
        try:
            threading.Thread(target=self._run, args=(argv,), daemon=True).start()
        except RuntimeError as exc:                  # thread could not start
            self.busy.clear()
            self.sink((f"Could not start command: {exc}", S_ERROR))
            return False
        return True

    def _run(self, argv):
        cmd = [self.paths.cli, "--script", self.paths.script,
               "--config", self.paths.config] + argv
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                self.sink((line.rstrip("\n"), S_OUT))
            proc.wait()
        except OSError as exc:
            self.sink((f"Failed to run putioctl: {exc}", S_ERROR))
        finally:
            self.busy.clear()


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

class Paths:
    def __init__(self, args):
        self.cli = args.cli
        self.script = args.script
        self.config = args.config
        self.log = args.log
        self.lockfile = args.lock
        self.pause_file = args.pause
        self.mount_point = args.mount or ""
        self.local_dir = args.local_dir or ""
        self.progress_dir = args.progress_dir or ""
        self.schedule = args.schedule or ""
        self.hide_idle = bool(args.hide_idle)


class Console:
    def __init__(self, stdscr, paths):
        self.stdscr = stdscr
        self.paths = paths
        self.lines = []              # [(text, style)]
        self.wrapped = []            # [(text, style)] after wrapping to width
        self.wrap_width = 0
        self.scroll = 0              # lines scrolled up from the bottom
        self.input = ""
        self.cursor = 0
        self.history = []
        self.history_pos = None
        self.pending_quit = False
        self.message = ""
        self.blink_epoch = time.time()
        self.events = queue.Queue()
        self.probe = StateProbe(paths)
        self.journal = TransferJournal()
        self.runner = CommandRunner(paths, self.events.put)
        self.tail = LogTail(paths.log)
        self.hide_idle = paths.hide_idle

    # -- buffer ------------------------------------------------------------

    def add(self, text, style=S_NORMAL, kind=K_UI):
        """Buffer a line. `kind` separates log lines from console chatter, so
        the latter can be cleared away without losing any of the log."""
        self.lines.append((text, style, kind))
        if len(self.lines) > MAX_BUFFER_LINES:
            drop = len(self.lines) - MAX_BUFFER_LINES
            del self.lines[:drop]
            self.wrapped = []        # offsets shifted; rebuild lazily
            self.wrap_width = 0
            return
        if self.wrap_width and not self.is_hidden(kind):
            self.wrapped.extend(self._wrap(text, style, self.wrap_width))

    def add_transfer(self, row, note):
        """Buffer one of the journal's bars, flattened to a single line.

        The strip paints a transfer in four styles; the buffer holds one style
        per line, so the whole line goes in as S_TAG — which also makes the
        persisted bars read as a thread of their own rather than as a strip
        that somehow got stuck in the scrollback. Fitted to the width at the
        moment it is emitted, exactly as a log line arrives at whatever length
        the processor wrote it; a later resize rewraps it like any other.

        It carries the processor's own `[YYYY-MM-DD HH:MM:SS]` stamp, because
        the strip's line does not need one and a line in the scrollback does:
        without it two samples an hour apart sit adjacent and read as
        consecutive, so an overnight stall — the thing worth scrolling back
        for — is invisible between two bars that both look live. The stamp is
        taken out of the width before the bar is fitted, so the line still
        ends where every other line ends.
        """
        stamp = time.strftime("[%Y-%m-%d %H:%M:%S] ")
        width = max(1, self.stdscr.getmaxyx()[1] - len(stamp))
        line = stamp + "".join(text for text, _ in transfer_segments(row, width, note))
        self.add(line.rstrip(), S_TAG, K_XFER)
        if self.scroll:
            self.scroll += 1          # keep the reader's place, as log lines do

    def clear_chatter(self):
        """Drop command echoes and their output, keeping the log itself.

        What you typed and what it printed are transient; the log is the thing
        you came to watch. This puts the pane back to a pure tail without
        throwing away scrollback the way a full clear would.
        """
        self.lines = [line for line in self.lines if line[2] != K_UI]
        self.wrap_width = 0          # force a rewrap of what's left
        self.scroll = 0
        self.stdscr.clearok(True)

    def clear_all(self):
        self.lines, self.wrapped, self.wrap_width = [], [], 0
        self.scroll = 0
        self.stdscr.clearok(True)

    def _clamp_scroll(self, height=1):
        """Keep the scrollback offset inside the buffer that exists right now.

        `height` is the visible pane, so scrolling to the very top leaves a
        full screen of the oldest lines rather than a single row stranded at
        the top of an empty pane.
        """
        self.scroll = max(0, min(self.scroll, max(0, len(self.wrapped) - height)))

    def _wrap(self, text, style, width):
        if width <= 1:
            return [(text, style)]
        text = text.replace("\t", "    ")
        if len(text) <= width:
            return [(text, style)]
        out, indent = [], "  "
        out.append((text[:width], style))
        rest = text[width:]
        step = max(1, width - len(indent))
        while rest:
            out.append((indent + rest[:step], style))
            rest = rest[step:]
        return out

    def _rewrap(self, width):
        self.wrapped = []
        for text, style, kind in self.lines:
            if self.is_hidden(kind):
                continue
            self.wrapped.extend(self._wrap(text, style, width))
        self.wrap_width = width

    def is_hidden(self, kind):
        return kind == K_IDLE and self.hide_idle

    def kind_for(self, line):
        return K_IDLE if IDLE_RE.search(line) else K_LOG

    def set_hide_idle(self, hide):
        """Flip the filter and rebuild the pane from the untouched buffer."""
        if hide == self.hide_idle:
            return
        self.hide_idle = hide
        self.wrap_width = 0          # force a rewrap under the new filter
        self.scroll = 0
        self.stdscr.clearok(True)

    def style_for(self, line):
        m = LEVEL_RE.match(line)
        level = m.group(1) if m else ""
        if level == "ERROR":
            return S_ERROR
        if level == "WARN":
            return S_WARN
        if TAG_RE.search(line):
            return S_TAG
        return S_NORMAL

    # -- rendering ---------------------------------------------------------

    def draw(self):
        h, w = self.stdscr.getmaxyx()
        if w < 20 or h < 6:
            return
        show_hint = h >= 12
        show_bar = h >= 9
        # Bars only where there is room to spare: on a short window the log is
        # what the console is for, and the status bar still says a run is going.
        rows = self.probe.transfers[:MAX_TRANSFER_ROWS] if h >= MIN_HEIGHT_FOR_TRANSFERS else []
        chrome = 3 + (1 if show_bar else 0) + (1 if show_hint else 0) + len(rows)
        pane_h = max(1, h - chrome)

        if self.wrap_width != w:
            self._rewrap(w)

        self.stdscr.erase()
        self._draw_pane(0, pane_h, w)
        row = pane_h
        for transfer in rows:
            self._draw_transfer(row, w, transfer)
            row += 1
        if show_bar:
            self._draw_bar(row, w)
            row += 1
        self._draw_input(row, w)
        row += 3
        if show_hint:
            self._draw_hint(row, w)
        self.stdscr.refresh()

    def _addstr(self, y, x, text, attr=0):
        # Writing to the last cell of a line is an error in curses; clip instead.
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[:max(0, w - x - 1)]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _draw_pane(self, top, height, width):
        total = len(self.wrapped)
        # Clamp here rather than only where scrolling happens: the buffer can
        # shrink underneath a scroll offset captured when it was larger —
        # widening the terminal rewraps long lines into fewer rows, and trimming
        # at MAX_BUFFER_LINES drops rows outright. An unclamped offset makes
        # `end` negative, and the slice below then renders the oldest lines in
        # the buffer instead of where the reader actually is.
        self._clamp_scroll(height)
        end = total - self.scroll
        start = max(0, end - height)
        for i, (text, style) in enumerate(self.wrapped[start:end]):
            self._addstr(top + i, 0, text, self.attr(style))
        if self.scroll > 0:
            note = f"  ▲ scrolled back {self.scroll} lines · PgDn or Ctrl-L to follow "
            self._addstr(top + height - 1, max(0, width - len(note) - 1), note,
                         self.attr(S_ACCENT) | curses.A_BOLD)

    def _draw_transfer(self, row, width, transfer):
        x = 0
        for text, style in transfer_segments(transfer, width):
            self._addstr(row, x, text, self.attr(style))
            x += len(text)

    def _draw_bar(self, row, width):
        paused, until, reason = self.probe.pause_state()
        pid = self.probe.running_pid()

        if paused:
            state = "PAUSED"
            if until:
                state += f" {human_delta(until - time.time())} left"
            if reason:
                state += f" · {reason}"
        else:
            state = "active"

        run = f"running (pid {pid})" if pid else "idle"

        bits = [state, run]
        nxt = next_cron_run(self.paths.schedule)
        if nxt and not paused:
            # 12-hour to match the CLI; lstrip drops the hour's leading zero.
            clock = time.strftime('%I:%M %p', time.localtime(nxt)).lstrip('0')
            bits.append(f"next {clock} in {human_delta(nxt - time.time(), precise=True)}")
        if self.probe.free_space:
            bits.append(f"{self.probe.free_space} free")
        if self.probe.mount_ok is False:
            bits.append("MOUNT DOWN")
        if self.hide_idle:
            bits.append("quiet mode")
        # Whatever the strip above couldn't show — because the window is short,
        # or because more transfers are in flight than it draws — is at least
        # counted here, and `progress` prints the detail.
        count = len(self.probe.transfers)
        shown = min(count, MAX_TRANSFER_ROWS) \
            if self.stdscr.getmaxyx()[0] >= MIN_HEIGHT_FOR_TRANSFERS else 0
        if count > shown:
            bits.append(f"{count} transfer{'s' if count > 1 else ''}")

        text = " " + " · ".join(bits)
        text += " " * max(0, width - len(text) - 1)
        alert = paused or self.probe.mount_ok is False
        attr = self.attr(S_BAR_ALERT if alert else S_BAR)
        self._addstr(row, 0, text, attr)

    def _draw_input(self, row, width):
        dim = self.attr(S_DIM)
        inner = max(1, width - 4)
        self._addstr(row, 0, "╭" + "─" * (width - 3) + "╮", dim)
        self._addstr(row + 2, 0, "╰" + "─" * (width - 3) + "╯", dim)
        self._addstr(row + 1, 0, "│", dim)
        self._addstr(row + 1, width - 2, "│", dim)

        prompt = "› "
        # Scroll the visible slice so the cursor stays on screen on long input.
        avail = inner - len(prompt)
        offset = max(0, self.cursor - avail + 1)
        visible = self.input[offset:offset + avail]
        self._addstr(row + 1, 2, prompt, self.attr(S_ACCENT) | curses.A_BOLD)
        self._addstr(row + 1, 2 + len(prompt), visible, self.attr(S_NORMAL))

        # The block cursor is painted here rather than left to the terminal:
        # whether the hardware cursor blinks is a per-terminal preference we
        # can't set portably, so it's drawn as reverse video on the character
        # underneath and toggled on a timer.
        cur_x = 2 + len(prompt) + (self.cursor - offset)
        if self.cursor_lit():
            under = self.input[self.cursor] if self.cursor < len(self.input) else " "
            self._addstr(row + 1, cur_x, under, self.attr(S_NORMAL) | curses.A_REVERSE)
        try:
            self.stdscr.move(row + 1, cur_x)
        except curses.error:
            pass

    def _draw_hint(self, row, width):
        # Two lengths rather than one clipped line: a hint that gets truncated
        # loses its tail, and the tail is where the important keys live.
        full = ("type 'help' for commands · ↑↓ history · Tab complete · PgUp scroll · "
                "Ctrl-L redraw · Ctrl-C quit")
        short = "'help' for commands · ↑↓ history · Tab complete · Ctrl-C quit"
        text = self.message or (full if width >= len(full) + 4 else short)
        style = S_WARN if self.message else S_DIM
        self._addstr(row, 2, text[:max(0, width - 3)], self.attr(style))

    def attr(self, style):
        return self.styles.get(style, 0)

    def cursor_lit(self):
        """Whether the cursor block is drawn on this frame.

        The phase resets on every keystroke, so the cursor is solid the moment
        you type and only starts blinking once you pause — typing can never
        land on a dark frame and leave you hunting for the caret.
        """
        return int((time.time() - self.blink_epoch) / BLINK_SECONDS) % 2 == 0

    # -- input -------------------------------------------------------------

    def handle_key(self, key):
        self.blink_epoch = time.time()      # keep the cursor solid while typing
        if isinstance(key, str):
            code = ord(key) if len(key) == 1 else None
            if code == 3:                                 # Ctrl-C
                return self._interrupt()
            if code == 4:                                 # Ctrl-D
                return False if not self.input else True
            if code in (10, 13):
                self.submit()
                return True
            if code == 12:                                # Ctrl-L
                # Follow again, and force a full repaint. curses only sends
                # the cells that changed, so anything that corrupts the screen
                # from outside — a stray write to the same tty, a dropped SSH
                # connection — stays corrupt until something redraws every
                # cell. That is the conventional job of Ctrl-L, and it is the
                # only key that can repair the display.
                self.scroll = 0
                self.wrap_width = 0
                self.stdscr.clearok(True)
                return True
            if code == 21:                                # Ctrl-U
                self.input, self.cursor = self.input[self.cursor:], 0
                return True
            if code == 11:                                # Ctrl-K
                self.input = self.input[:self.cursor]
                return True
            if code == 1:                                 # Ctrl-A
                self.cursor = 0
                return True
            if code == 5:                                 # Ctrl-E
                self.cursor = len(self.input)
                return True
            if code == 23:                                # Ctrl-W
                head = self.input[:self.cursor].rstrip()
                cut = head.rfind(" ") + 1
                self.input = self.input[:cut] + self.input[self.cursor:]
                self.cursor = cut
                return True
            if code == 9:                                 # Tab
                self.complete()
                return True
            if code in (8, 127):                          # Backspace
                if self.cursor:
                    self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
                    self.cursor -= 1
                return True
            if code is not None and code < 32:
                return True
            self.input = self.input[:self.cursor] + key + self.input[self.cursor:]
            self.cursor += len(key)
            self.pending_quit = False
            return True

        # Special keys arrive as ints.
        if key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.input), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.input)
        elif key == curses.KEY_BACKSPACE:
            if self.cursor:
                self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.input = self.input[:self.cursor] + self.input[self.cursor + 1:]
        elif key == curses.KEY_UP:
            self.recall(-1)
        elif key == curses.KEY_DOWN:
            self.recall(1)
        elif key == curses.KEY_PPAGE:
            self.scroll += 10
            self._clamp_scroll()
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
        elif key == curses.KEY_RESIZE:
            self.wrap_width = 0
        return True

    def _interrupt(self):
        if self.input:
            self.input, self.cursor = "", 0
            self.pending_quit = False
            return True
        if self.pending_quit:
            return False
        self.pending_quit = True
        self.message = "Press Ctrl-C again to quit (the sync keeps running)"
        return True

    def recall(self, direction):
        if not self.history:
            return
        if self.history_pos is None:
            self.history_pos = len(self.history)
        self.history_pos = max(0, min(len(self.history), self.history_pos + direction))
        self.input = "" if self.history_pos == len(self.history) else self.history[self.history_pos]
        self.cursor = len(self.input)

    def complete(self):
        head = self.input[:self.cursor]
        parts = head.split()
        pool = COMMANDS
        if len(parts) > 1 and parts[0] == "config":
            pool = CONFIG_SUBCOMMANDS
        elif len(parts) > 1 or head.endswith(" "):
            return
        prefix = parts[-1] if parts and not head.endswith(" ") else ""
        matches = [c for c in pool if c.startswith(prefix)]
        if len(matches) == 1:
            completion = matches[0][len(prefix):] + " "
            self.input = head + completion + self.input[self.cursor:]
            self.cursor += len(completion)
        elif matches:
            self.message = "  ".join(matches)

    # -- commands ----------------------------------------------------------

    def submit(self):
        raw = self.input.strip()
        self.input, self.cursor = "", 0
        self.history_pos = None
        self.pending_quit = False
        self.message = ""
        self.scroll = 0
        if not raw:
            return
        self.history.append(raw)
        self.add(f"› {raw}", S_CMD)

        try:
            argv = shlex.split(raw.lstrip("/"))
        except ValueError as exc:
            self.add(f"  {exc}", S_ERROR)
            return
        if not argv:
            return

        cmd = argv[0]
        if cmd in ("quit", "exit", "q"):
            raise SystemExit(0)
        if cmd == "clear":
            # Default keeps the log: the usual reason to clear is that command
            # output is in the way of the tail, not that the tail is unwanted.
            if len(argv) > 1 and argv[1] in ("all", "-a", "--all"):
                self.clear_all()
            else:
                self.clear_chatter()
            return
        if cmd == "help":
            self.show_help()
            return
        if cmd == "logs":
            self.add("  The pane above already streams the log; use PgUp to scroll back.", S_OUT)
            return
        if cmd == "quiet":
            # Handled here, not by the CLI alone: the pane keeps idle lines in
            # its buffer and merely stops drawing them, so the toggle applies to
            # scrollback that already streamed past. The CLI is still run, with
            # an explicit on/off rather than a toggle, to persist the setting
            # for the next console and for `putioctl logs`.
            arg = argv[1] if len(argv) > 1 else ""
            if arg == "status":
                self.runner.run(argv)
                return
            if arg in ("", "toggle"):
                want = not self.hide_idle
            elif arg in ("on", "hide", "yes", "true"):
                want = True
            elif arg in ("off", "show", "no", "false"):
                want = False
            else:
                self.add(f"  quiet: expected on, off or status — got '{arg}'", S_ERROR)
                return
            # Persist first: the runner refuses while another command is in
            # flight, and a pane that hides lines the config still shows would
            # quietly undo itself on the next console.
            if not self.runner.run(["quiet", "on" if want else "off"]):
                return
            self.set_hide_idle(want)
            self.add("  Idle \"nothing to do\" runs hidden." if want
                     else "  Showing every completed run again.", S_OUT)
            return
        if cmd == "progress":
            # Every option this command has turns watch mode on, which would
            # repaint on a timer inside the pane and hold the command slot for
            # good. The strip above the status bar is the live view; the CLI
            # gets run for one snapshot in the log.
            if len(argv) > 1 and argv[1] not in ("-h", "--help"):
                self.add("  The strip above the status bar updates on its own — "
                         "printing one snapshot instead.", S_OUT)
                argv = ["progress"]
        if cmd == "sync":
            # --follow would have the CLI stream the same log this pane is
            # already showing, and would block the command slot until the run
            # ends. The pane is the follow.
            argv = [a for a in argv if a not in ("-f", "--follow")]
        if cmd not in COMMANDS:
            self.add(f"  Unknown command: {cmd} — type 'help' for the list.", S_ERROR)
            return

        if (cmd, argv[1] if len(argv) > 1 else "") in INTERACTIVE:
            if self.runner.busy.is_set():
                self.add("  A command is still running — wait for it to finish.", S_WARN)
                return
            self.add("  Opening your editor; the console returns when you close it.", S_OUT)
            self.draw()
            self.run_foreground(argv)
            return

        self.runner.run(argv)

    def run_foreground(self, argv):
        """Drop out of curses, give the command the terminal, then take it back.

        Runs on the UI thread by necessity: it tears the screen down and
        rebuilds it, which no background thread may do while the main loop is
        drawing. Log lines written while the editor is up are picked up by the
        normal tail on the next tick.
        """
        cmd = [self.paths.cli, "--script", self.paths.script,
               "--config", self.paths.config] + argv
        note = ("", S_NORMAL)

        # def_prog_mode/reset_prog_mode is the pair that makes this reliable:
        # it snapshots our terminal modes before handing them over and puts
        # them back afterwards. Re-asserting raw() and friends by hand is not
        # enough — the editor leaves the tty in its own state and input stops
        # reaching us.
        curses.def_prog_mode()
        curses.endwin()
        try:
            rc = subprocess.call(cmd)
            if rc != 0:
                note = (f"  exited with status {rc}", S_WARN)
        except OSError as exc:
            note = (f"  Failed to run putioctl: {exc}", S_ERROR)
        finally:
            curses.reset_prog_mode()
            curses.flushinp()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.stdscr.clear()
            self.wrap_width = 0      # force a full rewrap and repaint
            self.stdscr.refresh()
        if note[0]:
            self.add(*note)

    def show_help(self):
        for line in [
            "  status                    current state, schedule and storage",
            "  sync [--force]            start a run now (progress appears above)",
            "  pause [30m|2h|1d] [-m …]  hold off scheduled runs",
            "  resume                    re-enable scheduled runs",
            "  stop [-t N]               terminate the run in progress",
            "  progress                  print a snapshot of the transfers in flight",
            "  config list|get|set|…     inspect and change settings",
            "  quiet [on|off]            hide the idle 'nothing to do' runs in the log above",
            "  clear [all]               remove command output, keeping the log (all = wipe)",
            "  quit                      leave the console (the sync keeps running)",
            "",
            "  Keys: ↑↓ history · Tab complete · PgUp/PgDn scroll · Ctrl-L redraw+follow · Ctrl-C quit",
        ]:
            self.add(line, S_OUT)

    # -- main loop ---------------------------------------------------------

    def setup_styles(self):
        curses.start_color()
        curses.use_default_colors()
        # (foreground, background); -1 means the terminal's own background.
        # The status bar names both colours rather than using A_REVERSE:
        # reverse paints the *terminal's* background colour as the text, which
        # on a dark theme is black on blue and barely legible. Naming both
        # gives the same band with predictable contrast in any theme.
        pairs = {
            S_DIM:       (curses.COLOR_WHITE,   -1),
            S_ERROR:     (curses.COLOR_RED,     -1),
            S_WARN:      (curses.COLOR_YELLOW,  -1),
            S_TAG:       (curses.COLOR_CYAN,    -1),
            S_CMD:       (curses.COLOR_GREEN,   -1),
            S_OUT:       (curses.COLOR_CYAN,    -1),
            S_ACCENT:    (curses.COLOR_MAGENTA, -1),
            S_BAR:       (curses.COLOR_WHITE,   curses.COLOR_BLUE),
            S_BAR_ALERT: (curses.COLOR_WHITE,   curses.COLOR_RED),
        }
        self.styles = {S_NORMAL: curses.A_NORMAL}
        for idx, (style, (fg, bg)) in enumerate(pairs.items(), start=1):
            curses.init_pair(idx, fg, bg)
            attr = curses.color_pair(idx)
            if style == S_DIM:
                attr |= curses.A_DIM
            if style in (S_CMD, S_BAR, S_BAR_ALERT):
                attr |= curses.A_BOLD
            self.styles[style] = attr

    def run(self):
        self.setup_styles()
        try:
            # Hide the hardware cursor — the blinking block in the input box is
            # drawn by _draw_input, and two cursors in one cell look broken.
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        self.stdscr.timeout(POLL_INTERVAL_MS)
        # Raw mode so Ctrl-C arrives as a keystroke instead of a SIGINT. The
        # console must be able to answer it itself — one press clears the input
        # line, two in a row leave — rather than having the terminal kill us
        # mid-transfer.
        curses.raw()

        # Scrollback first, banner last: the banner is what the eye lands on,
        # so it belongs next to the prompt rather than 200 lines above it.
        for line in self.tail.prime(200):
            self.add(line, self.style_for(line), self.kind_for(line))
        self.add("", S_NORMAL)
        self.add(f"putioctl console — streaming {self.paths.log}", S_ACCENT)
        self.add("Type a command below; 'help' for the list, 'quit' to leave.", S_DIM)

        last_draw = 0.0
        while True:
            for line in self.tail.read_new():
                kind = self.kind_for(line)
                self.add(line, self.style_for(line), kind)
                # Keep the reader's place while scrolled back — but only for
                # lines that actually landed in the pane.
                if self.scroll and not self.is_hidden(kind):
                    self.scroll += 1
            while True:
                try:
                    text, style = self.events.get_nowait()
                except queue.Empty:
                    break
                self.add("  " + text, style)
            for row, note in self.journal.update(self.probe.transfers):
                self.add_transfer(row, note)

            now = time.time()
            if now - last_draw >= 0.1:
                self.draw()
                last_draw = now

            try:
                key = self.stdscr.get_wch()
            except curses.error:
                continue                    # timeout: loop to poll the log again
            except KeyboardInterrupt:
                if not self._interrupt():
                    return
                continue
            if not self.handle_key(key):
                return
            self.draw()
            last_draw = time.time()


def main():
    parser = argparse.ArgumentParser(description="Interactive console for the put.io sync")
    parser.add_argument("--cli", required=True, help="path to the putioctl shell CLI")
    parser.add_argument("--script", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--pause", required=True)
    parser.add_argument("--mount", default="")
    parser.add_argument("--local-dir", default="")
    parser.add_argument("--progress-dir", default="",
                        help="directory the processor publishes transfer progress in")
    parser.add_argument("--schedule", default="")
    parser.add_argument("--hide-idle", action="store_true",
                        help="start with idle 'nothing to do' runs filtered out")
    args = parser.parse_args()

    locale.setlocale(locale.LC_ALL, "")
    # Ctrl-C is read as a keystroke (see curses.raw() below). This only covers a
    # SIGINT arriving from elsewhere, e.g. `kill -INT`, which should not leave
    # the terminal in curses mode.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    paths = Paths(args)
    console = None

    def bootstrap(stdscr):
        nonlocal console
        console = Console(stdscr, paths)
        console.run()

    try:
        curses.wrapper(bootstrap)
    except SystemExit:
        pass
    finally:
        if console:
            console.probe.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
