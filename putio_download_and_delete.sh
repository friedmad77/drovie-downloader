#!/usr/bin/env bash
#
# putio_download_and_delete.sh — the entry point cron fires.
#
# This used to be the processor. Since phase 5 of the port it is a shim: it
# resolves the binaries, sources .putio_config, and execs into
# `python3 -m putio run`, which is putio/run.py. Everything between the pause
# gate and the summary line is Python now.
#
# It stays at this path, with this name, because cron's crontab entry,
# `putioctl`'s script resolution and `putioctl deploy` all name it — keeping
# the path meant none of them had to change.
#
# Four things are still bash, and each is here for a reason rather than
# because it was left behind:
#
#   1. PATH and binary discovery. Shell-shaped, and it has to happen before
#      there is an interpreter to ask.
#   2. Sourcing .putio_config. The file is bash and gets *sourced* — values
#      reference $HOME and nothing stops one using command substitution — so a
#      Python reader would be a second implementation of bash's semantics that
#      has to agree with the first one forever. The shell the file is written
#      in reads it, once, and passes the answers on.
#   3. Mount recovery, and `--mount-gate`, which is how Python reaches it. The
#      plan's phase 6 recommends never porting this: it is osascript, Finder,
#      mount_smbfs, TCP probes, cross-run backoff files and dialog
#      suppression, and Python would still be shelling out to all of it.
#   4. `log`, because (3) logs, and the log format is an invariant shared with
#      putio/logs.py. Two writers, one format.
#
# The exec matters. It replaces the process image and *keeps the pid*, so the
# lockfile, `putioctl stop`'s pgrep walk and every `dl.<runpid>.<jobid>`
# progress file go on naming the same number they always did.

# Configuration and PATH setup
#
# Homebrew comes first. It used to sit after /usr/bin, which meant cron resolved
# a different rclone (/usr/local/bin, from the official installer) and a
# different jq (/usr/bin, Apple's) than an interactive shell did — so transfers
# ran on a build that was never exercised by hand. Only jq and rclone actually
# move as a result of this ordering; of the binaries Homebrew provides here, the
# only names that shadow a system one are jq, openssl, stdbuf and zsh, and this
# script calls none of the last three. Check that again before adding a formula
# that installs an unprefixed GNU tool: this script is written against BSD
# semantics (`stat -f %m`, `date -r`) and would break quietly under GNU ones.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$PATH"

# Ensure HOME is set for cron environments
# Resolved from the password database rather than hardcoded: a tilde with no
# name after it falls back to getpwuid() when HOME is unset, on both macOS and
# Linux, so this names the invoking user's home wherever the script runs.
if [ -z "$HOME" ]; then
    HOME=$(cd ~ 2>/dev/null && pwd)
    [ -n "$HOME" ] || HOME=$(eval echo "~$(id -un)")
    export HOME
fi

# Ensure rclone can find its config in cron
export RCLONE_CONFIG="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"

# Find rclone executable
RCLONE_CMD=$(which rclone 2>/dev/null)
if [ -z "$RCLONE_CMD" ]; then
    # Try common locations
    for path in /opt/homebrew/bin/rclone /usr/local/bin/rclone /usr/bin/rclone; do
        if [ -x "$path" ]; then
            RCLONE_CMD="$path"
            break
        fi
    done
fi

if [ -z "$RCLONE_CMD" ]; then
    echo "ERROR: rclone not found in PATH. Please install rclone or update PATH."
    exit 1
fi

# Find jq the same way, and for the same reason: so the config can pin it and
# cron stops depending on whatever PATH happens to resolve. Ordered Homebrew
# first to match the PATH above.
JQ_CMD=$(which jq 2>/dev/null)
if [ -z "$JQ_CMD" ]; then
    for path in /opt/homebrew/bin/jq /usr/local/bin/jq /usr/bin/jq; do
        if [ -x "$path" ]; then
            JQ_CMD="$path"
            break
        fi
    done
fi

if [ -z "$JQ_CMD" ]; then
    echo "ERROR: jq not found in PATH. Please install jq or update PATH."
    exit 1
fi

# Find a Python interpreter for the components under putio/.
#
# Deliberately NOT resolved with `which python3`. Homebrew is first in the PATH
# above, but the versioned formula (python@3.13, the only kind safe to depend
# on — the unversioned `python` moves under you at the next major release)
# installs no `python3` symlink at all, so `which python3` returns Apple's
# /usr/bin/python3: 3.9.6, frozen, and past upstream EOL. That is the same trap
# that once had cron running a different rclone and jq than an interactive
# shell, so it is resolved the same way — by absolute path, newest first, with
# the config free to pin it outright:
#
#   putioctl config set PYTHON_BIN /opt/homebrew/bin/python3.13
#
# Since phase 5 this is as fatal as rclone and jq, and the change is worth
# stating: through phases 1-4 every Python call site had a bash fallback
# behind it, so a missing interpreter cost the features that needed it and the
# sync carried on. Those fallbacks are gone — there is one implementation of
# each rule now, which is the point of finishing — so an interpreter that has
# gone away stops the sync, and it must say so at startup rather than at the
# first thing that needed it.
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    for path in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                /opt/homebrew/bin/python3.11 /usr/local/bin/python3.13 \
                /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
                /usr/bin/python3; do
        if [ -x "$path" ]; then
            PYTHON_BIN="$path"
            break
        fi
    done
fi

# Load configuration file - REQUIRED
# Try local .putio_config first, then fall back to ~/.putio_config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script's own path, so the Python download loop can re-enter it for the
# one thing that stays in bash — `--mount-gate`, below. Resolved here rather
# than from $0, which cron and `putioctl sync` spell differently.
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
# The run's pid. Since phase 5 this is simply *true* rather than arranged: the
# exec at the bottom keeps the pid, so $$ here and os.getpid() there are one
# number. It is still passed explicitly, because it is a contract and not an
# implementation detail — every progress file is named dl.<runpid>.<jobid>, and
# every reader calls `kill -0` on that number to tell a live transfer from what
# a killed run left frozen on screen.
RUN_PID=$$
if [ -f "$SCRIPT_DIR/.putio_config" ]; then
    CONFIG_FILE="$SCRIPT_DIR/.putio_config"
elif [ -f "$HOME/.putio_config" ]; then
    CONFIG_FILE="$HOME/.putio_config"
else
    echo "ERROR: Configuration file not found"
    echo "Looked for .putio_config in: $SCRIPT_DIR and $HOME"
    echo "Please create .putio_config in the script directory or ~/.putio_config"
    exit 1
fi

source "$CONFIG_FILE"

# The Drovie webhook was PLEX_REQUEST_WEBHOOK_URL/_KEY before the rename. The
# old names stay as aliases so an existing config keeps notifying Drovie; the
# new name wins when both are set.
DROVIE_WEBHOOK_URL="${DROVIE_WEBHOOK_URL:-${PLEX_REQUEST_WEBHOOK_URL:-}}"
DROVIE_WEBHOOK_KEY="${DROVIE_WEBHOOK_KEY:-${PLEX_REQUEST_WEBHOOK_KEY:-}}"

# Re-check the binaries now. The guards above ran against whatever PATH
# resolved, but the config is sourced afterwards and may pin either one to an
# absolute path — so a typo, or a path a Homebrew relocation moved, was never
# checked by anything. It surfaced only at first use, as every download logging
# "Could not determine size" with nothing at startup naming the cause.
for _bin in RCLONE_CMD JQ_CMD; do
    if [ ! -x "${!_bin}" ]; then
        echo "ERROR: $_bin is set to '${!_bin}', which is not executable."
        echo "Check the pin in $CONFIG_FILE."
        exit 1
    fi
done
unset _bin

# PYTHON_BIN gets the same re-check, and since phase 5 it gets the same verdict
# for being *absent* as for being wrong. The message names both cures, because
# the two causes need different ones: a pin that no longer resolves (a Homebrew
# relocation, a typo) and no interpreter on the machine at all.
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: no Python interpreter found, and the processor is Python since phase 5."
    echo "Install one and pin it, e.g.:"
    echo "  brew install python@3.13"
    echo "  putioctl config set PYTHON_BIN /opt/homebrew/bin/python3.13"
    exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: PYTHON_BIN is set to '$PYTHON_BIN', which is not executable."
    echo "Check the pin in $CONFIG_FILE."
    exit 1
fi

# rclone tuning. These are interpolated straight into the rclone command line,
# so a config file that omits one would otherwise pass an empty flag value —
# `--bwlimit=""` makes rclone exit 2 ("empty string") and every download fails.
# Defaults match server_config_template.
RCLONE_TRANSFERS="${RCLONE_TRANSFERS:-4}"
RCLONE_CHECKERS="${RCLONE_CHECKERS:-8}"
RCLONE_BUFFER_SIZE="${RCLONE_BUFFER_SIZE:-64M}"
RCLONE_MULTI_THREADS="${RCLONE_MULTI_THREADS:-2}"
RCLONE_MAX_CONCURRENT="${RCLONE_MAX_CONCURRENT:-1}"
RCLONE_BWLIMIT="${RCLONE_BWLIMIT:-off}"

# Live transfer progress. Each download publishes its state under
# PROGRESS_STATE_DIR so `putioctl progress` and the console can draw a bar for a
# run they didn't start; see the "Transfer progress" section below. Set
# PROGRESS_STATE_DIR empty to switch the feature off entirely. The stats
# interval is how often rclone appends a line to the per-download log, so it is
# both the refresh rate of the bar and what that file costs: roughly 700 bytes
# per tick, deleted when the transfer ends.
PROGRESS_STATE_DIR="${PROGRESS_STATE_DIR-/tmp/putio_progress}"
RCLONE_STATS_INTERVAL="${RCLONE_STATS_INTERVAL:-5s}"

# Mount-recovery tuning. Defaults are chosen so a NAS that disappears costs at
# most one Finder prompt and then goes quiet; see the mount section below for
# what each one gates. Override any of these in .putio_config.
MEDIA_MOUNT_STATE_DIR="${MEDIA_MOUNT_STATE_DIR:-/tmp/putio_mount_state}"
MOUNT_PROBE_PORT="${MOUNT_PROBE_PORT:-445}"
MOUNT_PROBE_TIMEOUT="${MOUNT_PROBE_TIMEOUT:-5}"
MOUNT_REMOUNT_BACKOFF_BASE="${MOUNT_REMOUNT_BACKOFF_BASE:-300}"
MOUNT_REMOUNT_BACKOFF_MAX="${MOUNT_REMOUNT_BACKOFF_MAX:-3600}"
MOUNT_REMOUNT_LOCK_STALE="${MOUNT_REMOUNT_LOCK_STALE:-300}"
MOUNT_REMOUNT_WAIT_SECONDS="${MOUNT_REMOUNT_WAIT_SECONDS:-240}"
ALLOW_GUI_REMOUNT="${ALLOW_GUI_REMOUNT:-true}"
MOUNT_GUI_MAX_FAILURES="${MOUNT_GUI_MAX_FAILURES:-2}"
MOUNT_GUI_SUPPRESS_SECONDS="${MOUNT_GUI_SUPPRESS_SECONDS:-21600}"
MOUNT_DISMISS_AUTH_DIALOGS="${MOUNT_DISMISS_AUTH_DIALOGS:-true}"

# ---------------------------------------------------------------------------
# The Python handoff
#
# Components under putio/ are called from here and read their settings from the
# environment. That direction is the whole point: bash has already sourced
# .putio_config — which is bash, and can contain command substitution,
# arithmetic and conditionals — so re-implementing a parser for it in Python
# would mean owning two readers of one file that must agree. There is exactly
# one reader, and it is the shell that the file is written in.
#
# The values are passed on the Python process's own environment rather than
# exported globally, because TMDB_API_KEY and PLEX_TOKEN are in the list and
# rclone, curl and unrar have no business inheriting them.
#
# PUTIO_PY_VARS is the contract. putio/config.py reads these names and no
# others; tests/progress_harness.sh fails if the two lists drift apart. Add a
# name here and in Config._FIELDS together.
#
# One asymmetry to know about: the shell cannot distinguish "unset" from "set
# to empty" once a value is on a child's environment, so Python treats an empty
# string as absent and falls back to its own default. The single place that
# matters is PROGRESS_STATE_DIR, where empty is a real setting meaning "publish
# no progress files" — config.py spells that case out rather than defaulting it.
PUTIO_PY_VARS="
REMOTE LOCAL_DIR STAGING_DIR USE_TEMP_DIR MOVIE_LIBRARY TV_LIBRARY UNMATCHED_DIR
MEDIA_MOUNT_POINT RUN_PID SCRIPT_PATH CONFIG_FILE LOG_FILE LOG_LEVEL LOCKFILE PAUSE_FILE
PROGRESS_STATE_DIR RCLONE_STATS_INTERVAL
RCLONE_CMD JQ_CMD PYTHON_BIN
RCLONE_TRANSFERS RCLONE_CHECKERS RCLONE_BUFFER_SIZE RCLONE_MULTI_THREADS
RCLONE_MAX_CONCURRENT RCLONE_BWLIMIT
DELETE_AFTER_DOWNLOAD MIN_FREE_SPACE_GB MAX_RETRIES RETRY_DELAY
TMDB_API_KEY PLEX_URL PLEX_TOKEN PLEX_MOVIE_LIBRARY_ID PLEX_TV_LIBRARY_ID
DROVIE_WEBHOOK_URL DROVIE_WEBHOOK_KEY
MAX_REQUESTS_PER_WINDOW RATE_WINDOW_SECONDS RATE_LIMIT_DELAY
"

# Run a putio subcommand: putio_python classify < listing
#
# Returns 127 without an interpreter, the same code the shell uses for a
# command it cannot find, so a caller can tell "Python is missing" from "the
# subcommand failed".
#
# It runs from SCRIPT_DIR rather than wherever cron happened to leave us, and
# that is load-bearing, not tidiness: for `python -m`, sys.path[0] is the
# working directory and it comes BEFORE PYTHONPATH. Setting PYTHONPATH alone
# means any directory that happens to contain a putio/ wins over the one beside
# this script. PYTHONPATH is still set, for the case where SCRIPT_DIR is not
# readable as a working directory. (`-P` would say this outright, but it needs
# Python 3.11 and Apple's 3.9 is still a possible interpreter here.)
#
# -B keeps a __pycache__ out of ~/Scripts: the deployed tree is replaced file by
# file, and a stale .pyc beside a swapped .py silently shadows it — which is not
# hypothetical, it happened while this function was being written.
#
# Never call this inside a per-item loop. An interpreter start is ~30ms and the
# lesson is already written down elsewhere in this file: per-line subprocesses
# once cost 36s of every idle run. Batch the whole list through one call.
putio_python() {
    if [ -z "$PYTHON_BIN" ]; then
        return 127
    fi
    local _v
    local _env=()
    for _v in $PUTIO_PY_VARS; do
        _env+=("$_v=${!_v}")
    done
    (
        cd "$SCRIPT_DIR" 2>/dev/null || true
        env "${_env[@]}" PYTHONPATH="$SCRIPT_DIR" "$PYTHON_BIN" -B -m putio "$@"
    )
}

# Logging configuration
LOG_LEVEL="${LOG_LEVEL:-INFO}"  # Default to INFO, can be overridden

# Log level hierarchy: ERROR=1, WARN=2, INFO=3, DEBUG=4
get_log_level_num() {
    case "$1" in
        "ERROR") echo 1 ;;
        "WARN")  echo 2 ;;
        "INFO")  echo 3 ;;
        "DEBUG") echo 4 ;;
        *) echo 3 ;;
    esac
}

# Rotate log file to keep only the last 24 hours of entries
rotate_log() {
    if [ ! -f "$LOG_FILE" ]; then
        return 0
    fi
    
    local temp_log="/tmp/putio_log_rotate_$$"
    local cutoff_epoch=$(($(date +%s) - 86400))  # 24 hours in seconds

    # The cutoff is rendered in the log's own timestamp format so awk can
    # compare it as a string: "YYYY-MM-DD HH:MM:SS" is zero-padded and
    # big-endian, so lexical order is chronological order. The previous
    # implementation parsed each line back into an epoch, which cost a sed and
    # up to two date processes per line — around 10s of every run on a log of a
    # few thousand lines, on a script cron fires every five minutes.
    local cutoff
    cutoff=$(date -r "$cutoff_epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null ||
             date -d "@$cutoff_epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)

    # Untimestamped lines (raw rclone stderr, multi-line continuations) inherit
    # the retention decision of the most recent timestamped line, so ancient
    # rclone output can't ride along forever. Until the first timestamped line
    # is seen, untimestamped lines are dropped. An empty cutoff — no working
    # date — compares greater than nothing and so keeps the whole log, which is
    # the right way to fail.
    awk -v cutoff="$cutoff" '
        /^\[[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]\]/ {
            keeping = (substr($0, 2, 19) >= cutoff)
            if (keeping) print
            next
        }
        keeping { print }
    ' "$LOG_FILE" > "$temp_log"

    # Replace the original log file if we successfully created a filtered version
    if [ -f "$temp_log" ]; then
        if [ -s "$temp_log" ]; then
            mv "$temp_log" "$LOG_FILE"
        else
            # If filtered result is empty, create empty log file
            > "$LOG_FILE"
            rm -f "$temp_log"
        fi
    fi
}

# Enhanced logging function with level filtering and log rotation
log() {
    local level="INFO"
    local context=""
    
    # Parse optional log level and context
    if [[ "$1" =~ ^(DEBUG|INFO|WARN|ERROR)$ ]]; then
        level="$1"
        shift
    fi
    
    if [[ "$1" =~ ^\[.*\]$ ]]; then
        context="$1 "
        shift
    fi
    
    # Check if we should log this level
    local current_level_num=$(get_log_level_num "$LOG_LEVEL")
    local message_level_num=$(get_log_level_num "$level")
    
    if [ "$message_level_num" -le "$current_level_num" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $context$*" | tee -a "$LOG_FILE"
        
        # Rotate log file periodically (every 100 log entries to avoid excessive processing)
        if [ $((RANDOM % 100)) -eq 0 ]; then
            rotate_log
        fi
    fi
}

# Run a command with a wall-clock timeout. Uses gtimeout/timeout when present
# (homebrew coreutils), otherwise falls back to a bash background+kill so this
# works on stock macOS too. Exit code 124 on timeout, otherwise the command's
# own exit code.
with_timeout() {
    local seconds="$1"
    shift

    if command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$seconds" "$@"
        return $?
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
        return $?
    fi

    "$@" &
    local pid=$!
    local waited=0
    while [ "$waited" -lt "$seconds" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid"
            return $?
        fi
        sleep 1
        waited=$((waited + 1))
    done
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    return 124
}

# --- Mount recovery ---------------------------------------------------------
#
# Everything below exists to make an SMB drop *quiet*. The failure mode being
# defended against: the NAS goes away, every code path that touches the share
# calls remount_media, each remount shells out to `open smb://`, and macOS
# stacks up one Finder auth dialog per call — several at once from the parallel
# download subshells, then a fresh batch every 5 minutes from cron — until
# someone notices and clicks through the whole pile.
#
# Four gates prevent that:
#   1. Reachability. If the NAS doesn't answer on port 445 we never hand an
#      smb:// URL to Finder at all. No server, no dialog. This is the common
#      case (laptop off the network, NAS asleep or rebooting).
#   2. Serialization. A state-dir lock means the concurrent download subshells
#      make one remount attempt between them rather than one apiece.
#   3. Backoff. A failed remount is recorded on disk, so subsequent cron
#      firings wait 5m, 10m, 20m ... instead of retrying every 5 minutes for
#      as long as the NAS stays down.
#   4. GUI suppression. After MOUNT_GUI_MAX_FAILURES consecutive failures we
#      stop invoking Finder entirely and just log what needs fixing — this
#      covers the "NAS is reachable but the saved password no longer works"
#      case, which is the one that produces actual username/password prompts.
#
# The silent mount_smbfs path is tried before Finder every time, so a recovery
# that can happen without a dialog does happen without a dialog.

mount_state_read() {
    local n
    n=$(cat "$MEDIA_MOUNT_STATE_DIR/$1" 2>/dev/null)
    case "$n" in
        ''|*[!0-9]*) n=0 ;;
    esac
    printf '%s' "$n"
}

# Same, but for state that isn't a counter (the failure-reason tag).
mount_state_read_raw() {
    cat "$MEDIA_MOUNT_STATE_DIR/$1" 2>/dev/null
}

mount_state_write() {
    mkdir -p "$MEDIA_MOUNT_STATE_DIR" 2>/dev/null
    printf '%s' "$2" > "$MEDIA_MOUNT_STATE_DIR/$1" 2>/dev/null
}

mount_state_bump() {
    mkdir -p "$MEDIA_MOUNT_STATE_DIR" 2>/dev/null
    printf '%s' "$(( $(mount_state_read "$1") + 1 ))" > "$MEDIA_MOUNT_STATE_DIR/$1" 2>/dev/null
}

mount_state_stamp() {
    mkdir -p "$MEDIA_MOUNT_STATE_DIR" 2>/dev/null
    date +%s > "$MEDIA_MOUNT_STATE_DIR/$1" 2>/dev/null
}

# Hostname to probe and to hand to mount_smbfs. MEDIA_MOUNT_URL normally
# carries a Bonjour service-instance name (host._smb._tcp.local) which
# getaddrinfo cannot resolve — only Finder/NetFS can — so rewrite it to the
# plain mDNS hostname. Set MEDIA_MOUNT_HOST in the config to override.
media_mount_host() {
    if [ -n "${MEDIA_MOUNT_HOST:-}" ]; then
        printf '%s' "$MEDIA_MOUNT_HOST"
        return 0
    fi
    local host="${MEDIA_MOUNT_URL#smb://}"
    host="${host%%/*}"   # drop /share
    host="${host#*@}"    # drop user@
    host="${host%%:*}"   # drop :port
    case "$host" in
        *._smb._tcp.local) host="${host%._smb._tcp.local}.local" ;;
    esac
    printf '%s' "$host"
}

# Gate 1. A plain TCP connect to the SMB port, which never touches Finder and
# never prompts for anything.
is_nas_reachable() {
    local host
    host=$(media_mount_host)
    if [ -z "$host" ]; then
        return 1
    fi
    if command -v nc >/dev/null 2>&1; then
        with_timeout $((MOUNT_PROBE_TIMEOUT + 3)) \
            nc -z -G "$MOUNT_PROBE_TIMEOUT" -w "$MOUNT_PROBE_TIMEOUT" \
            "$host" "$MOUNT_PROBE_PORT" >/dev/null 2>&1
        return $?
    fi
    with_timeout "$MOUNT_PROBE_TIMEOUT" \
        bash -c "exec 3<>/dev/tcp/$host/$MOUNT_PROBE_PORT" >/dev/null 2>&1
}

# `reason` records *why* the attempt failed: "unreachable" means the NAS never
# answered, "mount" means it answered but the share would not come back. The
# distinction matters because a backoff earned while the NAS was simply absent
# is meaningless once it starts answering again.
record_remount_failure() {
    local reason="${1:-mount}"
    mount_state_bump fail_count
    mount_state_stamp last_failure
    mount_state_write last_reason "$reason"
    log WARN "[MOUNT] Remount failure #$(mount_state_read fail_count) recorded ($reason)"
}

# Drop the retry cooloff without touching the Finder-prompt budget. Used when
# the NAS becomes reachable again: the wait was earned under conditions that no
# longer hold, but any auth failures it saw are still worth remembering, so a
# flapping NAS can't reopen the dialog allowance on every bounce.
clear_remount_backoff() {
    rm -f "$MEDIA_MOUNT_STATE_DIR/fail_count" \
          "$MEDIA_MOUNT_STATE_DIR/last_failure" \
          "$MEDIA_MOUNT_STATE_DIR/last_reason" 2>/dev/null
}

record_remount_success() {
    clear_remount_backoff
    rm -f "$MEDIA_MOUNT_STATE_DIR/gui_attempts" \
          "$MEDIA_MOUNT_STATE_DIR/gui_last_attempt" 2>/dev/null
}

# Gate 3. Exponential cooloff after a failed remount, persisted so it spans
# cron runs rather than resetting every 5 minutes.
remount_backoff_active() {
    local last n wait_for age i
    last=$(mount_state_read last_failure)
    n=$(mount_state_read fail_count)
    if [ "$last" -eq 0 ] || [ "$n" -lt 1 ]; then
        return 1
    fi

    wait_for="$MOUNT_REMOUNT_BACKOFF_BASE"
    i=1
    while [ "$i" -lt "$n" ] && [ "$wait_for" -lt "$MOUNT_REMOUNT_BACKOFF_MAX" ]; do
        wait_for=$((wait_for * 2))
        i=$((i + 1))
    done
    if [ "$wait_for" -gt "$MOUNT_REMOUNT_BACKOFF_MAX" ]; then
        wait_for="$MOUNT_REMOUNT_BACKOFF_MAX"
    fi

    age=$(( $(date +%s) - last ))
    if [ "$age" -lt "$wait_for" ]; then
        log INFO "[MOUNT] In remount backoff after failure #$n — ${age}s elapsed of ${wait_for}s, not retrying"
        return 0
    fi
    return 1
}

# Gate 4. Whether we're still allowed to involve Finder, which is the only
# thing here that can raise a username/password dialog.
gui_remount_allowed() {
    if [ "${ALLOW_GUI_REMOUNT:-true}" != "true" ]; then
        log INFO "[MOUNT] Finder remount disabled by config (ALLOW_GUI_REMOUNT=false)"
        return 1
    fi

    local attempts last age
    attempts=$(mount_state_read gui_attempts)
    if [ "$attempts" -lt "$MOUNT_GUI_MAX_FAILURES" ]; then
        return 0
    fi

    last=$(mount_state_read gui_last_attempt)
    age=$(( $(date +%s) - last ))
    if [ "$age" -ge "$MOUNT_GUI_SUPPRESS_SECONDS" ]; then
        log INFO "[MOUNT] Finder-remount suppression expired after ${age}s — allowing one more attempt"
        return 0
    fi

    log ERROR "[MOUNT] Suppressing Finder remount: $attempts consecutive failures, no auth dialogs will be raised for another $(( MOUNT_GUI_SUPPRESS_SECONDS - age ))s"
    log ERROR "[MOUNT] Fix manually: Finder > Go > Connect to Server > $MEDIA_MOUNT_URL, then tick 'Remember this password in my keychain'"
    return 1
}

# Close the SMB auth dialog our own `open` left on screen. NetAuthAgent owns
# those windows and launchd relaunches it on demand, so killing it clears the
# prompt instead of leaving it for the user to click through.
#
# The kill is session-wide, so it is gated on `preexisting`: the caller records
# whether NetAuthAgent was already running before we invoked `open`. If it was,
# the dialog on screen belongs to something the user started themselves — a
# different server, a Time Machine target — and killing it would discard what
# they were typing. In that case we leave it alone and accept our own prompt
# lingering; the GUI-suppression gate still stops the pile-up.
dismiss_stale_mount_dialogs() {
    local preexisting="${1:-1}"

    if [ "${MOUNT_DISMISS_AUTH_DIALOGS:-true}" != "true" ]; then
        return 0
    fi
    if [ "$preexisting" -ne 0 ]; then
        log INFO "[MOUNT] Leaving NetAuthAgent alone — it was already running before our remount, so the dialog on screen isn't ours"
        return 0
    fi
    # The mount may have landed between the wait expiring and here; killing the
    # agent mid-authentication would abort it.
    if is_mount_responsive; then
        return 0
    fi
    if pgrep -x NetAuthAgent >/dev/null 2>&1; then
        log INFO "[MOUNT] Dismissing the SMB auth dialog our remount raised (killing NetAuthAgent)"
        pkill -x NetAuthAgent 2>/dev/null || true
    fi
}

# Single-shot liveness probe: mount-table check plus one short stat. Used by
# the polling loop below, which doesn't need is_mount_alive's slow double-stat
# retry — that retry exists to avoid declaring a merely-slow share dead, and a
# loop that re-probes every few seconds already gives the share that leeway.
is_mount_responsive() {
    if [ -z "$MEDIA_MOUNT_POINT" ]; then
        return 0  # Feature disabled
    fi
    mount | grep -q " on $MEDIA_MOUNT_POINT " || return 1
    with_timeout 5 stat -f "$MEDIA_MOUNT_POINT" >/dev/null 2>&1
}

# Poll until the share is usable again, up to `limit` seconds of wall clock.
# The budget is a real deadline, not an iteration count: the probe itself can
# block, so counting passes would let a nominal 30s wait run for minutes while
# holding both the remount lock and the script lockfile.
wait_for_mount() {
    local limit="$1"
    local deadline
    deadline=$(( $(date +%s) + limit ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if is_mount_responsive; then
            return 0
        fi
        # This loop runs inside the locked section, and a lock left untouched
        # past MOUNT_REMOUNT_LOCK_STALE gets broken by a sibling — which would
        # let a second Finder prompt through. Keep it fresh while we work.
        remount_lock_touch
        sleep 3
    done
    return 1
}

# Non-interactive mount. mount_smbfs is a CLI tool: it never raises a Finder
# dialog, and with stdin closed it can't prompt on a terminal either — it just
# fails. Only viable when the mountpoint directory survived the unmount, since
# /Volumes is root-owned and we can't recreate it, and when the password is
# still in the login keychain.
try_mount_smbfs() {
    command -v mount_smbfs >/dev/null 2>&1 || return 1
    [ -d "$MEDIA_MOUNT_POINT" ] || return 1
    if mount | grep -q " on $MEDIA_MOUNT_POINT "; then
        return 1
    fi

    local host share user url
    host=$(media_mount_host)
    [ -n "$host" ] || return 1
    share="${MEDIA_MOUNT_URL##*/}"
    [ -n "$share" ] || return 1

    user="${MEDIA_MOUNT_URL#smb://}"
    case "$user" in
        *@*) user="${user%%@*}" ;;
        *)   user="" ;;
    esac
    if [ -n "$user" ]; then
        url="//${user}@${host}/${share}"
    else
        url="//${host}/${share}"
    fi

    log INFO "[MOUNT] Trying silent mount_smbfs $url"
    with_timeout 30 mount_smbfs "$url" "$MEDIA_MOUNT_POINT" </dev/null >/dev/null 2>&1
}

# Returns 0 if MEDIA_MOUNT_POINT is currently mounted and responsive. The
# mount-table check catches the case where the share has dropped but the
# /Volumes/<name> directory still exists as a stale stub; the timed stat
# catches the case where the kernel still thinks it's mounted but I/O hangs.
# A single slow stat over mDNS-resolved SMB isn't enough to declare the
# mount dead — retry once with a longer timeout before giving up, otherwise
# a momentary NAS hiccup aborts the whole run.
is_mount_alive() {
    if [ -z "$MEDIA_MOUNT_POINT" ]; then
        return 0  # Feature disabled
    fi
    if ! mount | grep -q " on $MEDIA_MOUNT_POINT "; then
        log DEBUG "[MOUNT] $MEDIA_MOUNT_POINT not in mount table"
        return 1
    fi
    if with_timeout 15 stat -f "$MEDIA_MOUNT_POINT" >/dev/null 2>&1; then
        return 0
    fi
    log DEBUG "[MOUNT] First stat -f on $MEDIA_MOUNT_POINT timed out after 15s, retrying"
    sleep 2
    if with_timeout 15 stat -f "$MEDIA_MOUNT_POINT" >/dev/null 2>&1; then
        return 0
    fi
    log WARN "[MOUNT] stat -f on $MEDIA_MOUNT_POINT timed out twice (mount table shows share present)"
    return 1
}

# Gate 2. mkdir is atomic, so the concurrent download subshells can't each
# start their own remount. A lock left behind by a killed subshell is broken
# once it ages past MOUNT_REMOUNT_LOCK_STALE — but a live holder refreshes the
# mtime as it works (see wait_for_mount), so only a dead one ages out.
#
# The lock carries an owner token because breaking is still possible: without
# it, a holder whose lock was taken over would later rmdir the *new* holder's
# lock on its way out, and both would end up running.
REMOUNT_LOCK_TOKEN=""

# Unique per process, including the parallel download subshells, which all
# share $$. $BASHPID would do it but doesn't exist in bash 3.2 (stock macOS),
# so ask a child for its parent's pid instead.
remount_lock_new_token() {
    sh -c 'echo $PPID' 2>/dev/null
}

remount_lock_claim() {
    REMOUNT_LOCK_TOKEN="$1"
    printf '%s' "$1" > "$MEDIA_MOUNT_STATE_DIR/remount.lock/owner" 2>/dev/null
    return 0
}

remount_lock_acquire() {
    mkdir -p "$MEDIA_MOUNT_STATE_DIR" 2>/dev/null

    local token
    token=$(remount_lock_new_token)
    [ -n "$token" ] || token="$$"

    if mkdir "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null; then
        remount_lock_claim "$token"
        return 0
    fi

    local mtime age
    mtime=$(stat -f %m "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null)
    case "$mtime" in
        ''|*[!0-9]*) return 1 ;;
    esac
    age=$(( $(date +%s) - mtime ))
    if [ "$age" -gt "$MOUNT_REMOUNT_LOCK_STALE" ]; then
        log WARN "[MOUNT] Breaking stale remount lock (${age}s old, holder stopped refreshing it)"
        rm -f "$MEDIA_MOUNT_STATE_DIR/remount.lock/owner" 2>/dev/null
        rmdir "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null
        if mkdir "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null; then
            remount_lock_claim "$token"
            return 0
        fi
    fi
    return 1
}

# Keep a held lock from ageing into "stale" while we're legitimately working.
remount_lock_touch() {
    if [ -n "$REMOUNT_LOCK_TOKEN" ] && [ -d "$MEDIA_MOUNT_STATE_DIR/remount.lock" ]; then
        touch "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null || true
    fi
}

remount_lock_release() {
    if [ -z "$REMOUNT_LOCK_TOKEN" ]; then
        return 0
    fi
    local owner
    owner=$(cat "$MEDIA_MOUNT_STATE_DIR/remount.lock/owner" 2>/dev/null)
    if [ -n "$owner" ] && [ "$owner" != "$REMOUNT_LOCK_TOKEN" ]; then
        log WARN "[MOUNT] Not releasing remount lock — another process took it over"
        REMOUNT_LOCK_TOKEN=""
        return 0
    fi
    rm -f "$MEDIA_MOUNT_STATE_DIR/remount.lock/owner" 2>/dev/null
    rmdir "$MEDIA_MOUNT_STATE_DIR/remount.lock" 2>/dev/null || true
    REMOUNT_LOCK_TOKEN=""
}

# Attempt to (re)mount the media share. Silent mount_smbfs first, Finder only
# as a gated fallback. Returns 0 once the mount is usable, 1 otherwise.
# Callers get at most one real attempt per run and per backoff window; see the
# gate summary at the top of this section.
remount_media() {
    if [ -z "$MEDIA_MOUNT_POINT" ] || [ -z "$MEDIA_MOUNT_URL" ]; then
        return 1
    fi
    # Recovery is mount_smbfs, Finder and NetAuthAgent: macOS only. Elsewhere a
    # dropped share is the host's to remount (fstab, systemd, the Docker
    # volume), and the gate above already defers each file until it is back.
    if [ "$(uname -s)" != "Darwin" ]; then
        log WARN "[MOUNT] $MEDIA_MOUNT_POINT is not responding; automatic remount is macOS-only, so remount it on the host"
        return 1
    fi

    # A sibling download subshell is already on it. Wait for its result rather
    # than firing a second attempt — this is what turns "one dialog per
    # concurrent job" into "one attempt, shared by all of them".
    if ! remount_lock_acquire; then
        log INFO "[MOUNT] Another process is remounting $MEDIA_MOUNT_POINT — waiting for its result"
        local deadline timed_out=0
        deadline=$(( $(date +%s) + MOUNT_REMOUNT_WAIT_SECONDS ))
        while [ -d "$MEDIA_MOUNT_STATE_DIR/remount.lock" ]; do
            if [ "$(date +%s)" -ge "$deadline" ]; then
                timed_out=1
                break
            fi
            sleep 3
        done
        if [ "$timed_out" -eq 1 ]; then
            # Inconclusive, not failed: the holder is still working and may yet
            # succeed. Returning plain failure here would let the caller declare
            # the share dead and abort the run seconds before it recovers.
            log WARN "[MOUNT] Remount by another process still in progress after ${MOUNT_REMOUNT_WAIT_SECONDS}s — deferring rather than declaring the share dead"
            return 2
        fi
        is_mount_alive
        return $?
    fi

    remount_media_locked
    local rc=$?
    remount_lock_release
    return $rc
}

remount_media_locked() {
    # Re-check under the lock: whoever we queued behind may have fixed it.
    if is_mount_alive; then
        return 0
    fi

    # Gate 1 runs first and unconditionally. It costs one short TCP connect,
    # puts nothing on screen, and its answer is what tells us whether a backoff
    # inherited from an earlier outage still means anything. Checking the
    # backoff ahead of it would leave the share unmounted for up to
    # MOUNT_REMOUNT_BACKOFF_MAX after the NAS came back.
    if ! is_nas_reachable; then
        log ERROR "[MOUNT] $(media_mount_host):$MOUNT_PROBE_PORT unreachable — skipping remount, no auth prompts raised"
        record_remount_failure unreachable
        return 1
    fi

    # The NAS is answering again. A cooloff earned purely because it was absent
    # was earned under conditions that no longer hold, so drop it and retry now
    # instead of sitting out the rest of the window.
    if [ "$(mount_state_read_raw last_reason)" = "unreachable" ]; then
        log INFO "[MOUNT] $(media_mount_host) is reachable again — clearing the backoff from that outage"
        clear_remount_backoff
    fi

    # Gate 3. What survives is a cooloff earned by a remount that failed with
    # the NAS present and answering — worth respecting, since retrying it is
    # what burns the Finder-prompt budget.
    if remount_backoff_active; then
        return 1
    fi

    log WARN "[MOUNT] Attempting to remount $MEDIA_MOUNT_POINT from $MEDIA_MOUNT_URL"

    # If the share is still listed in the mount table but I/O is hanging, a
    # fresh mount is a no-op — macOS thinks it's already mounted. Force-unmount
    # first so the remount actually re-establishes the share.
    if mount | grep -q " on $MEDIA_MOUNT_POINT "; then
        log INFO "[MOUNT] Force-unmounting stale $MEDIA_MOUNT_POINT before remount"
        if command -v diskutil >/dev/null 2>&1; then
            with_timeout 15 diskutil unmount force "$MEDIA_MOUNT_POINT" >/dev/null 2>&1 || \
                with_timeout 15 umount -f "$MEDIA_MOUNT_POINT" >/dev/null 2>&1 || true
        else
            with_timeout 15 umount -f "$MEDIA_MOUNT_POINT" >/dev/null 2>&1 || true
        fi
    fi

    # Silent path first — a recovery that can happen without a dialog should.
    if try_mount_smbfs && wait_for_mount 30; then
        log INFO "[MOUNT] $MEDIA_MOUNT_POINT is back online (mount_smbfs)"
        record_remount_success
        return 0
    fi

    # Finder knows how to resolve the Bonjour name and can recreate the
    # root-owned mountpoint, so it stays as a fallback — but it's also the only
    # thing here that can put a password prompt on screen, hence the gate.
    if gui_remount_allowed; then
        # Counted before the attempt: cleared by record_remount_success, so a
        # crash mid-attempt errs toward suppressing rather than re-prompting.
        mount_state_bump gui_attempts
        mount_state_stamp gui_last_attempt

        # Clear a stale/empty stub directory so Finder can recreate it.
        if [ -d "$MEDIA_MOUNT_POINT" ] && ! mount | grep -q " on $MEDIA_MOUNT_POINT "; then
            rmdir "$MEDIA_MOUNT_POINT" 2>/dev/null || true
        fi

        # Note whether an auth agent was already up, so we only ever dismiss a
        # dialog we caused rather than one the user opened themselves.
        local agent_preexisting=0
        pgrep -x NetAuthAgent >/dev/null 2>&1 && agent_preexisting=1

        log INFO "[MOUNT] Falling back to Finder remount ($MEDIA_MOUNT_URL)"
        open "$MEDIA_MOUNT_URL" >/dev/null 2>&1 || true

        if wait_for_mount 60; then
            log INFO "[MOUNT] $MEDIA_MOUNT_POINT is back online (Finder)"
            record_remount_success
            return 0
        fi

        # Whatever Finder put on screen is still sitting there unanswered.
        dismiss_stale_mount_dialogs "$agent_preexisting"
    fi

    log ERROR "[MOUNT] Failed to remount $MEDIA_MOUNT_POINT"
    record_remount_failure
    return 1
}

# Verify the mount is alive; attempt a remount if not. Returns 0 if the
# mount is usable on return, 1 otherwise.
ensure_mount_alive() {
    if is_mount_alive; then
        return 0
    fi
    log WARN "[MOUNT] $MEDIA_MOUNT_POINT is not responding"
    remount_media
}

# If this script is being sourced (e.g., for unit-testing individual functions
# in another shell), skip all runtime side effects below and return now. All
# function definitions above are available to the caller.
if (return 0 2>/dev/null); then
    return 0
fi

# --mount-gate: the one thing the Python download loop still asks bash for.
#
# putio/mount.py calls this once per file, because SMB recovery is osascript,
# Finder, mount_smbfs, TCP probes, cross-run backoff files and dialog
# suppression — the least portable code in the project, and Python would still
# be shelling out to every bit of it (the plan's phase 6 recommends never
# porting it). Re-entering the script costs one bash start, measured at ~70ms,
# in a loop whose items take minutes; it is not the per-item subprocess the
# batching rule forbids, which is about items cheaper than the fork.
#
# Nothing else in the run body executes: no lockfile, no pause gate, no EXIT
# trap. ensure_mount_alive's status is passed through unchanged, including 2
# ("another process is remounting"), because the caller has to defer that file
# rather than condemn the share for the rest of the run.
if [ "${1:-}" = "--mount-gate" ]; then
    if [ -z "$MEDIA_MOUNT_POINT" ]; then
        exit 0
    fi
    ensure_mount_alive
    exit $?
fi

# ---------------------------------------------------------------------------
# Hand over
#
# Everything above is what has to exist before Python can be reached, or what
# Python reaches back for. Below is the handover itself.
#
# `exec` rather than a call, and it is load-bearing rather than tidy: exec
# replaces this process image and keeps the pid. That pid is the run's identity
# everywhere outside this file —
#
#   * the lockfile holds it, and `putioctl status` reports it
#   * `putioctl stop` walks `pgrep -P` down from it to reach the rclone children
#   * every progress file is named dl.<runpid>.<jobid>, and readers `kill -0`
#     that number to tell a live transfer from what a killed run left behind
#
# — so forking would have made the shim's pid and the worker's pid two
# different numbers and every one of those would have needed a story. RUN_PID
# was already $$ before phase 5 for the same reason; it is now simply true
# rather than arranged.
#
# The environment is built the same way putio_python builds it, and for the
# same reason: TMDB_API_KEY and PLEX_TOKEN are in PUTIO_PY_VARS, and rclone,
# curl and unrar have no business inheriting them. `env` adds to the
# environment rather than replacing it, so PATH, HOME, RCLONE_CONFIG, TERM,
# CRON_DEBUG and PUTIO_IGNORE_PAUSE all pass through untouched.
#
# The cd and the -B are putio_python's, verbatim, and both were found by
# mutating the harness rather than by reasoning: for `python -m`, sys.path[0]
# is the *working directory* and it comes before PYTHONPATH, so any directory
# that happens to contain a putio/ would win over the one beside this script;
# and the deployed tree is replaced file by file, where a stale .pyc silently
# shadows the .py beside it.
_py_env=()
for _v in $PUTIO_PY_VARS; do
    _py_env+=("$_v=${!_v}")
done
# TERM is passed explicitly, and it is not decoration. It is how the run
# decides whether to print the [ENV] banner ("cron or non-interactive"), and
# bash and Python do not see the same value: cron sets no TERM at all, and
# bash then sets TERM=dumb itself — as an ordinary shell variable, *not* an
# exported one. So `[ -z "$TERM" ]` was false in bash while
# os.environ.get("TERM") is None in the child, and every idle cron run started
# printing nine lines where it used to print one. Handing bash's own value over
# makes the two agree.
cd "$SCRIPT_DIR" 2>/dev/null || true
exec env "${_py_env[@]}" TERM="$TERM" PYTHONPATH="$SCRIPT_DIR" "$PYTHON_BIN" -B -m putio run

# Only reachable if exec itself failed — a PYTHON_BIN that passed the -x check
# and then could not be executed at all (a wrong architecture, a bad
# interpreter line). exec leaves the shell running on failure, so without this
# the script would fall off the end and exit 0, and cron would report a
# perfectly successful run that did nothing whatsoever.
log ERROR "[RUN] Failed to exec $PYTHON_BIN -m putio run"
exit 1
