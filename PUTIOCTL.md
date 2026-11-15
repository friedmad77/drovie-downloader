# `putioctl` — controlling the put.io sync

The processor itself runs unattended from cron every 5 minutes. `putioctl` is
how you interact with it in between: check what it's doing, change a setting,
run a sync now, or hold the schedule off for a while.

It reads and writes exactly the state a scheduled run uses — the same config
file, lockfile, pause marker and log — so the CLI and cron can never disagree
about what is running or whether syncing is paused.

---

## Install

`putioctl` lives beside the processor in the **install folder** (the copy cron
runs). That is `~/.local/share/drovie-downloader` (or
`$XDG_DATA_HOME/drovie-downloader`) unless `DROVIE_DOWNLOADER_HOME` names
another; an older install already in the previous default folder is found and
kept where it is. To deploy the current repo version of both:

```bash
./putioctl deploy
```

That copies `putio_download_and_delete.sh`, `putioctl`, `putioctl_tui.py`,
`putio-statusd` and the `putio/` package into the install folder, and makes the scripts
executable. It never copies `.putio_config` — the repo copy carries
placeholder API keys, and overwriting the live config with it would break every
run. It also refuses to deploy while a sync is in progress.

The install folder isn't on `$PATH`, so either call it by path:

```bash
~/.local/share/drovie-downloader/putioctl status
```

or link it once:

```bash
ln -s ~/.local/share/drovie-downloader/putioctl ~/.local/bin/putioctl
```

The rest of this document assumes `putioctl` is on your `$PATH`.

---

## The console

Running `putioctl` with no arguments opens the interactive console: the log
streams in the upper pane, and a prompt at the bottom takes the same commands
the CLI does, with their output shown inline.

```
[13:24:47] [INFO] [PARALLEL] Queued 3 files for download (max concurrent: 1)
[13:24:52] [INFO] [DOWNLOAD] Starting: Some.Movie.2024.1080p.mkv (14.2GiB)
› pause 45m -m "movie night"
  ✓ Sync paused until 2026-08-14 2:09 PM (45m left) — movie night
  ⇣ Some.Movie.2024.1080p.mkv   ██████░░░░░░░░   41%  5.9 GiB / 14.2 GiB · 22.4 MiB/s · ETA 6m 21s
─────────────────────────────────────────────────────────────────────────────
 PAUSED 44m left · movie night · running (pid 28694) · 991Gi free
╭───────────────────────────────────────────────────────────────────────────╮
│ › sync                                                                    │
╰───────────────────────────────────────────────────────────────────────────╯
  type 'help' for commands · ↑↓ history · Tab complete · PgUp scroll · Ctrl-C quit
```

Between the log and the status bar sits a line per download in flight, with a
bar that moves as the bytes land — see [`progress`](#progress--whats-moving-right-now)
for what the numbers mean. It appears when a transfer starts and disappears when
the last one finishes; on a window shorter than 14 rows the log keeps those rows
and the status bar shows a `2 transfers` count instead.

That strip is a live view and redraws in place, so **a copy of the bar is also
left in the log pane**, stamped like every other line — when the transfer is
first seen, every 10% it crosses, if it restarts from zero on a retry, when it
moves from downloading to copying into the library, and once more, marked
`· ended`, when it leaves the strip. Scroll back the next morning and the
night's downloads are still there in order, about a dozen lines each:

```
[2026-03-04 03:11:02] [INFO] [DOWNLOAD] Starting: Some.Movie.2024.1080p.mkv (14.2GiB)
[2026-03-04 03:14:38]   ⇣ Some.Movie.2024.1080p.mkv   ███░░░░░░░░░░░   20%  2.9 GiB / 14.2 GiB · 22.4 MiB/s
[2026-03-04 03:16:12]   ⇣ Some.Movie.2024.1080p.mkv   ████░░░░░░░░░░   30%  4.3 GiB / 14.2 GiB · 22.1 MiB/s
[2026-03-04 03:22:31]   ⇣ Some.Movie.2024.1080p.mkv   ██████████████  100%  14.2 GiB / 14.2 GiB · copying
[2026-03-04 03:22:39]   ⇣ Some.Movie.2024.1080p.mkv   ██████████████  100%  14.2 GiB / 14.2 GiB · copying · ended
[2026-03-04 03:22:40] [INFO] [ORGANIZE] Some Movie (2024).mkv → Movies/Some Movie (2024)
```

The stamp is the point of keeping them: two bars an hour apart would otherwise
sit next to each other and read as consecutive, and an overnight stall is
exactly what you scrolled back to find.

A bar that jumps **backwards** is rclone starting the file over — each retry
is a fresh process counting from zero — and gets a line of its own, so a
download that got to 85%, died and began again says so rather than going quiet
for hours.

`ended` says only that the bar is over: whether the file landed is the
processor's own line underneath it. `clear` keeps these — they are a record of
what happened, not console chatter — and `quiet` never hides them.

The status bar tracks pause state, the running PID, the next scheduled run
(counting down to the second, e.g. `next 2:15 PM in 3m 43s`), free space, and
shouts if the media mount has dropped. Log lines are coloured
by level — errors red, warnings yellow, tagged lines (`[MOUNT]`, `[PLEX]`,
`[PUTIO]`) cyan.

| Key | Does |
| --- | --- |
| `↑` / `↓` | Command history |
| `Tab` | Complete a command name (or list the matches) |
| `PgUp` / `PgDn` | Scroll the log back; a marker shows you've stopped following |
| `Ctrl-L` | Redraw the screen and jump back to following the live tail |
| `Ctrl-A/E/U/K/W` | Standard line editing at the prompt |
| `Ctrl-C` | Clears the input line; twice in a row exits |
| `quit` / `exit` | Leave the console — **the sync keeps running** |

Commands typed at the prompt are the CLI's own, so anything in the reference
below works there: `status`, `sync`, `pause 2h`, `resume`, `stop`, `progress`,
`config set KEY VALUE`, `quiet`. Two extras exist only in the console: `clear`
(below) and `help`, which lists the commands. `sync --follow` is accepted but the flag
is dropped — the pane is already the follow, and `progress --watch` likewise
prints one snapshot, because the strip above the status bar is already watching.

**Nothing takes over the screen.** Command output is printed into the same pane
as the log, so there is no mode to leave and no view to switch back to — the
log keeps streaming underneath whatever you just ran.

When a long result (`config list`, `status`) is in the way, **`clear` removes
the console's own output and leaves a pure log tail**. It
keeps every log line, including scrollback, and drops only what the console
put on screen: the banner, the commands you typed and what they printed.
`clear all` wipes the pane completely, log included. If you scrolled up with
PgUp, `Ctrl-L` drops you back to the live tail.

The one exception is `config edit`, which needs a terminal to itself. The
console steps aside, gives `$EDITOR` the full screen, and repaints itself when
you close the editor — log lines written while you were editing are picked up
on return.

Closing the console never touches a transfer in flight; it's a viewer, not the
process running the sync.

The console needs `python3` (stock macOS has it) and a terminal. Without either
— piped output, cron, a script — bare `putioctl` prints `status` instead, so
non-interactive callers are unaffected. `PUTIOCTL_NO_TUI=1` forces that
behaviour, and `putioctl tui` forces the console.

---

## Quick start (non-interactive)

Every command works directly from the shell, which is what you want in scripts
and one-liners:

```bash
putioctl status                       # what's happening right now
putioctl sync --follow                # run a sync and watch it
putioctl pause 2h -m "movie night"    # stop scheduled runs for two hours
putioctl resume                       # start them again
putioctl stop                         # kill the run in progress
putioctl progress -w                  # live bars for the downloads in flight
putioctl config set RCLONE_BWLIMIT 20M
putioctl logs -f --errors
putioctl quiet                        # hide the idle "nothing to do" runs
```

---

## Commands

### `status`

The default command. Shows, in one screen:

- whether syncing is **paused** (and until when, with the reason you gave)
- whether a run is **in progress** — its PID, elapsed time, and the latest log line
- a **bar for each download in flight**, the same numbers [`progress`](#progress--whats-moving-right-now) shows
- when the **last run completed**, and how many errors are in the retained log
- the **cron schedule**, and the next fire time for `*/N * * * *` schedules
- whether the **media mount** is up and how much **free space** is left
- where the **status service** Drovie reads (`putio-statusd`) listens, whether it
  answers `/healthz`, and the address to put in Drovie's *Drovie Downloader
  status address* field. The listening address comes from `STATUSD_BIND` /
  `STATUSD_PORT` when set (the container sets them), else from the installed
  launchd plist, else `127.0.0.1:9494`. It is not in `--json`, because
  `putio-statusd` serves that JSON
- which script, config and log file it is talking to

```
put.io sync status

  Active      scheduled runs are enabled
  Running     PID 28694, elapsed 04:11
              [2026-08-14 15:47:02] [INFO] [DOWNLOAD] Starting: Some.Movie.2024.1080p.mkv (14.2GiB)
  Transfer    Some.Movie.2024.1080p.mkv
              ██████████░░░░░░░░░░░░  41%  5.9 GiB / 14.2 GiB · 22.4 MiB/s · ETA 6m 21s
  Last run    completed 2026-08-14 3:50 PM
  Schedule    */5 * * * *
              next run 2026-08-14 3:55 PM (in 3m 43s)
  Mount       /Volumes/Media mounted
  Free space  991Gi available at /Volumes/Media/Put.io Downloads
  Status API  127.0.0.1:9494 answering
  For Drovie  http://host.docker.internal:9494 (Drovie in Docker)
              http://127.0.0.1:9494 (Drovie run directly)
```

On a Mac, Drovie tries exactly those two addresses on its own when its status
address field is blank, so it only needs filling in for a non-default port or a
Downloader on another machine.

### `sync` — run one now

```bash
putioctl sync              # start in the background, return immediately
putioctl sync --follow     # start it and stream the log
putioctl sync --force      # run even while paused
```

| Option | Effect |
| --- | --- |
| `-f`, `--follow` | Stream new log lines until the run finishes. **Ctrl-C detaches the viewer only** — the sync keeps running. Use `putioctl stop` to actually end it. |
| `--force` | Run once despite an active pause. The pause stays in effect for scheduled runs afterwards. |

It refuses to start a second run while one is in progress and tells you the PID
— the processor's lockfile enforces the same rule for cron, so a manual sync can
never collide with a scheduled one. Aliases: `run`, `now`.

The run's own stdout/stderr goes to `/tmp/putio_manual_run.log`; everything
interesting is in the normal log.

### `pause` / `resume` — gate the schedule

```bash
putioctl pause                        # until you resume
putioctl pause 90                     # bare number = minutes
putioctl pause 45m -m "on a call"     # 30s / 45m / 2h / 3d
putioctl resume
```

Durations accept `s`, `m`, `h`, `d`; a bare number means minutes. `-m` (or
`--message`) records a reason, shown in `status` and written to the log each
time a run is skipped.

**Pause does not stop a sync already running.** It gates *future* runs; the CLI
warns you and points at `putioctl stop` if a transfer is in flight when you
pause.

### `stop` — end the run in progress

```bash
putioctl stop
putioctl stop --timeout 60
```

Sends `SIGTERM` to the processor and every rclone child it spawned, waits (30s
by default, `-t`/`--timeout` to change), then `SIGKILL`s anything still alive.
It then clears the lockfile and per-run temp files, which a `SIGKILL`ed run
never gets to clean up itself. Partial downloads resume on the next run.

**Stop does not pause the schedule** — cron will start a fresh run at the next
tick. To go properly quiet, do both:

```bash
putioctl stop && putioctl pause 1h
```

Aliases: `cancel`, `kill`.

### `progress` — what's moving right now

```bash
putioctl progress            # one snapshot
putioctl progress -w         # redraw until Ctrl-C
putioctl progress -n 5       # ... every 5 seconds (implies -w)
```

```
put.io transfers

  Some.Movie.2024.1080p.mkv
  ███████████░░░░░░░░░░░░░░░░░  41%  5.9 GiB / 14.2 GiB · 22.4 MiB/s · ETA 6m 21s
```

The processor publishes what each download is doing as it goes, so this works
against a run cron started — it reads that state and never touches the transfer.
Nothing in flight is the normal answer, and it says which kind of nothing:
a run busy listing, extracting or organizing; a pause; or no run at all.

A download shows rclone's own byte count, speed and ETA. When `USE_TEMP_DIR` is
on, the finished file is then copied from the staging disk onto the media share,
which is a plain `cp` that reports nothing — that phase is measured by the size
of the file appearing at the far end and reads `copying to the library`, with no
speed or ETA.

The numbers refresh at `RCLONE_STATS_INTERVAL` (default `5s`), so `-n 1` redraws
the same figures five times over. A transfer that dies with its run disappears
on the next look rather than sitting frozen at 60%: the state files are stamped
with the run's PID, and readers skip any whose run is gone.

Alias: `transfers`.

### `config` — inspect and change settings

```bash
putioctl config list                   # all settings, API keys masked
putioctl config list --show-secrets    # unmasked
putioctl config get RCLONE_BWLIMIT
putioctl config set RCLONE_BWLIMIT 20M
putioctl config set DELETE_AFTER_DOWNLOAD false
putioctl config unset RCLONE_BWLIMIT   # fall back to the script's default
putioctl config edit                   # open in $EDITOR
putioctl config path
```

What it does for you:

- **Backs up first.** The previous file is always copied to
  `.putio_config.bak` before anything is written.
- **Validates before writing.** The new file must parse as valid bash or the
  edit is rejected — including after `config edit`, which restores the backup if
  you leave the file broken. This matters: a config with a syntax error makes
  *every* cron run die at startup, silently, until someone notices downloads
  stopped.
- **Preserves the file.** Comments and ordering stay intact; a setting that
  exists only as a commented-out example (the config ships several) is
  uncommented in place rather than duplicated at the bottom.
- **Masks secrets.** Anything whose name ends in `KEY`, `TOKEN`, `PASSWORD` or
  `SECRET` shows as `****6789` in `list` unless you pass `--show-secrets`, so
  the output is safe to paste somewhere.
- **Warns on new keys.** Setting a key that wasn't present says so — a typo'd
  name would otherwise look like it worked and change nothing.

Values are written inside double quotes with `$` left intact, so
`putioctl config set STAGING_DIR '$HOME/.putio_staging'` stays expandable, the
way the shipped config writes it.

Changes take effect on the **next** run. A sync already in progress keeps the
values it started with, and the CLI tells you when that's the case.

### `logs`

```bash
putioctl logs                # last 40 lines
putioctl logs -n 200
putioctl logs -f             # follow
putioctl logs --errors       # warnings and errors only
putioctl logs -g '\[PLEX\]'  # extended regex
putioctl logs --idle         # include idle runs even with quiet on
putioctl logs --quiet        # hide them just this once
```

The processor rotates this log to the last 24 hours at the start of each run,
so it stays small; anything older is gone.

### `quiet` — hide the idle runs

Cron fires every few minutes and most runs find nothing to do, each leaving one
line and nothing else:

```
[2026-08-22 15:45:02] [INFO] === Script completed successfully — nothing to do (1s) ===
```

On a five-minute schedule that is roughly 280 lines a day, and it is the bulk of
what `logs` shows. `quiet` filters those lines out of `putioctl logs` and the
console's log pane:

```bash
putioctl quiet          # toggle
putioctl quiet on       # or set it explicitly
putioctl quiet off
putioctl quiet status   # setting, plus how many idle lines the log holds
```

Only the idle summary is hidden. A run that downloaded, organised or failed is
untouched, and so is one that found files but downloaded none — that says
`nothing downloaded (3 file(s) on put.io)`, which is a different thing worth
seeing.

Nothing is removed from the log file: the lines are still there, and
`putioctl logs --idle` shows them whatever the setting is. When the filter
leaves nothing at all to print — a genuinely quiet day — the explanation goes to
stderr, so a redirected `putioctl logs` still contains log lines only. The setting is stored
as `LOGS_HIDE_IDLE` in the config so it persists, but the processor never reads
it — it changes only what you are shown.

In the console, `quiet` toggles the pane immediately, **including scrollback
that has already streamed past** — the lines stay in the buffer and are simply
not drawn, so toggling back brings them straight back. The status bar shows
`quiet mode` while the filter is on, so a suspiciously empty pane is always
explained.

### `tui`

Opens the console explicitly — the same thing bare `putioctl` does. Useful when
you've set `PUTIOCTL_NO_TUI=1`. Alias: `console`.

### `deploy`

```bash
putioctl deploy            # from the repo checkout
putioctl deploy ~/src/drovie-downloader    # or name the source explicitly
```

Copies `putio_download_and_delete.sh`, `putioctl`, `putioctl_tui.py`,
`putio-statusd` and the `putio/` package from the
repo to the install folder. The source is the path you give it, otherwise the current
directory when it holds the scripts, otherwise the directory `putioctl` itself
lives in — so it works from the repo even when `putioctl` is on `$PATH` via the
symlink above. Installs via a temp file and a rename rather than overwriting in
place — bash reads a script by offset as it executes, so overwriting the live
file mid-run would feed a running sync new bytes at an old offset. Refuses while
a sync is in progress. Never touches the config.

`putio/` is installed a file at a time rather than by swapping the directory:
swapping leaves an instant with no directory at all, and an import that lands in
it fails outright. Modules go in non-executable — they are imported, never run
directly — and `.py` files that no longer exist in the repo are pruned
afterwards, along with any `__pycache__`, since Python would otherwise go on
importing a module you deleted.

The recorded API responses under `putio/tests/fixtures/` ship with the modules
and are pruned the same way. They are not an optional extra: the suite lives
inside the package so it can be run against the deployed tree, and a deploy that
left the fixtures behind would have those tests error in the install folder while
passing in the checkout — which is precisely the difference the suite exists to
detect.

Replacing `putio-statusd` does not restart it; launchd is holding the old one
until you kick it (`launchctl kickstart -k gui/$UID/app.drovie.downloader.statusd`), and
`deploy` says so when the job is loaded.

---

## How pausing works

`putioctl pause` writes a marker file (`PAUSE_FILE`, default `~/.putio_paused`):

```bash
PAUSED_AT=1755183600
PAUSED_UNTIL=1755190800     # 0 means indefinite
PAUSED_REASON="movie night"
```

The processor checks it at startup, before it takes the lockfile, and exits
**0** with a line in the log:

```
[2026-08-14 16:00:41] [INFO] [PAUSE] Sync is paused until 2026-08-14 18:00 (movie night) — skipping run
```

Three deliberate properties:

- **The crontab is never modified.** The schedule stays intact, a pause is one
  file away from being undone, and a half-finished pause can't leave syncing
  permanently disabled.
- **Exit status is 0.** Cron mails you failures; a run you skipped on purpose
  isn't one.
- **Timed pauses clear themselves.** When `PAUSED_UNTIL` passes, the next run
  deletes the marker, logs `[PAUSE] Pause expired — resuming scheduled runs`,
  and proceeds. A pause you forget about lapses on its own; only a bare
  `putioctl pause` lasts until you resume.

Setting `PUTIO_IGNORE_PAUSE=1` bypasses the gate for a single run — that's what
`putioctl sync --force` uses. Deleting the marker file by hand is equivalent to
`putioctl resume`.

---

## Which copy am I controlling?

By default `putioctl` targets the **deployed** script,
`putio_download_and_delete.sh` in the install folder, because that's the one cron runs and
therefore the one whose config, lock and log describe the live sync. It then
finds the config the same way the script does: `.putio_config` beside the
script, falling back to `~/.putio_config`.

| Override | Effect |
| --- | --- |
| `--repo` | Act on the copy sitting beside `putioctl` (i.e. the repo checkout) |
| `--script PATH` | Control a specific processor script |
| `--config PATH` | Read/edit a specific config file |
| `PUTIO_SCRIPT`, `PUTIO_CONFIG` | Same as the two flags, as environment variables |

`putioctl status` always prints the three paths it resolved, so you can confirm
what you're pointed at.

---

## Files and state

`<install>` is the install folder described under [Install](#install).

| Path | What it is |
| --- | --- |
| `<install>/putio_download_and_delete.sh` | The processor cron runs |
| `<install>/putioctl`, `putioctl_tui.py` | The CLI and its interactive console |
| `<install>/putio/` | Python package the processor calls into; carries its own tests |
| `<install>/.putio_config` | Live configuration (API keys, paths, tuning) |
| `<install>/.putio_config.bak` | Automatic backup from the last config change |
| `<install>/putio_download_and_delete.log` | Processor log, rotated to 24 hours |
| `/tmp/putio_download_and_delete.lock` | Held for the duration of a run; contains the PID |
| `~/.putio_paused` | Pause marker (absent = not paused) |
| `/tmp/putio_manual_run.log` | stdout/stderr of a run started by `putioctl sync` |
| `/tmp/putio_mount_state` | SMB remount backoff counters and Finder-prompt suppression |
| `/tmp/putio_progress` | One `.meta` + rclone `.log` per download in flight; deleted as each finishes |

---

## Troubleshooting

**`status` says a sync is running but nothing is happening.** Check the last log
line it prints. A run stuck on a dead SMB mount will be sitting in the remount
path; `putioctl logs -n 50` shows the `[MOUNT]` lines. `putioctl stop` ends it.

**A transfer looks stuck.** `putioctl progress -w` answers it directly: bytes
that keep climbing mean it is fine however quiet the log is, and a bar that has
not moved in minutes with the mount up is a genuine stall — `putioctl stop` ends
it and the next run resumes the file.

**`progress` shows nothing while a big file is clearly downloading.** The bars
come from the *deployed* processor, so a copy that predates this feature
publishes nothing to draw: `putioctl deploy`, and the next run has bars. During a
run started by the old copy, the log is the only view.

**"stale lockfile present".** A previous run died without cleaning up. Harmless
— the next run detects the dead PID and breaks the lock itself. `putioctl stop`
clears it immediately.

**Nothing downloads and the log has no errors.** Check you aren't paused:
`putioctl status`. A paused run logs one `[PAUSE]` line per cron tick.

**A config change had no effect.** Values apply from the next run, and a sync
already in progress keeps the old ones. Confirm with `putioctl config get KEY`
that it landed in the file `putioctl config path` reports — editing the repo's
`.putio_config` does not affect the deployed script.

**Recovering a bad config.** `cp <install>/.putio_config.bak
<install>/.putio_config`. `config set` and `config edit` refuse to leave invalid
bash behind, but the backup is there for a change that was valid and wrong.

**Everything is off the rails.** The pause marker and the lockfile are the only
state that can hold the sync back, and both are just files:

```bash
rm -f ~/.putio_paused /tmp/putio_download_and_delete.lock
```

---

## Machine-readable status

`putioctl status --json` prints everything `status` draws, as one object —
in-flight transfers included, so a single call gives a coherent snapshot rather
than two that can disagree about whether a download just finished.
`putioctl progress --json` prints just the transfers array.

```bash
putioctl status --json | jq '{state, transfers: (.transfers | length)}'
```

The shape is a contract. Fields get added; `schema` is bumped only when one
changes meaning, because an old Drovie app build keeps polling long after the
server has moved on.

**These calls can block.** A transfer in its copy phase is measured by stat'ing
the media share, and free space runs `df` against it — both sit in the kernel
when the NAS drops. Poll them from something you can abandon, never from a
request handler.

## The status endpoint

`putio-statusd` is the always-on face of the above, so Drovie (and
the Drovie app behind it) can show live download progress. It binds **loopback only**:
Drovie runs on this same machine and already owns the internet-facing
edge through cloudflared, so nothing needs to reach this from off-box.

```bash
curl -s localhost:9494/status | jq .
curl -s localhost:9494/transfers | jq .
curl -s localhost:9494/healthz | jq .
```

Every response carries `stale_seconds`. When the most recent refresh failed the
last good reading is still served, with `source_error` saying why it stopped
updating — a momentarily unreachable share ages the data rather than blanking
it, and the caller decides when to stop trusting it. `/healthz` touches no
files at all.

### Installing it

launchd expands neither `~` nor `$HOME`, so the shipped plist carries an
`__INSTALL_DIR__` placeholder that has to be filled in on the way in. From the
repo folder:

```bash
putioctl statusd reinstall
```

Check it came up:

```bash
curl -s localhost:9494/healthz | jq .
```

**After a `putioctl deploy`**, restart it — replacing the file does not replace
the running process:

```bash
launchctl kickstart -k gui/$UID/app.drovie.downloader.statusd
```

**After a change to the plist itself**, `kickstart` is not enough: launchd
re-execs the job from the copy it already has in memory, so an edited
`EnvironmentVariables`, `ProgramArguments` or interval is silently ignored and
the daemon comes back with the old configuration — and reports success. Use:

```bash
putioctl statusd reinstall
```

which fills in `__INSTALL_DIR__`, installs the plist, unloads the running job,
waits for launchd to finish tearing it down, bootstraps the new one and checks
that it answers.

The plist is the one deployable that does not live in the install folder, so
`putioctl deploy` never installs it — applying a plist means taking the status
API down, which should be a decision rather than a side effect of copying
scripts. Deploy does compare the two and warns when they have drifted, so a
stale plist cannot sit there unnoticed.

The equivalent by hand, if putioctl is not available:

```bash
sed "s|__HOME__|$HOME|g" app.drovie.downloader.statusd.plist > ~/Library/LaunchAgents/app.drovie.downloader.statusd.plist
launchctl bootout gui/$UID/app.drovie.downloader.statusd 2>/dev/null
while launchctl print gui/$UID/app.drovie.downloader.statusd >/dev/null 2>&1; do sleep 0.1; done
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/app.drovie.downloader.statusd.plist
```

The wait is not decoration. `bootout` returns before launchd has finished
tearing the job down, and a `bootstrap` that lands in that window fails with
`Bootstrap failed: 5: Input/output error` — after the old job is already gone,
so the daemon stays down until someone notices and runs `bootstrap` again.
Measured at 0.2–0.4s. `bootout` is allowed to fail (hence `2>/dev/null`): if the
job was not loaded there is nothing to remove and the loop exits immediately.
That loop is the reason the command exists — it is the line a reader in a hurry
deletes.

Either way this needs a real login session: `bootstrap` into `gui/<uid>` fails
over a plain ssh connection with nobody logged in at the console.

To remove it entirely:

```bash
launchctl bootout gui/$UID/app.drovie.downloader.statusd && rm ~/Library/LaunchAgents/app.drovie.downloader.statusd.plist
```

Its own log is `<install>/putio-statusd.log`. It is a pure reader — if it dies,
or you never install it, the sync is completely unaffected.

Integrating something with it? `STATUS_API.md` is the consumer-facing reference:
response shapes, TypeScript types, nullability, failure modes, and how to build
a UI against a fake transfer without waiting for a real download.
