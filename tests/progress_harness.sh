#!/usr/bin/env bash
#
# Exercises the processor end to end against a local directory standing in for
# put.io. Nothing here reaches put.io, the media share, the real Plex, the real
# webhook or the live log.
#
#   tests/progress_harness.sh                    # the repo copy
#   tests/progress_harness.sh ~/Scripts/putio_download_and_delete.sh
#
# Since phase 5 the processor is a shim that execs into `python3 -m putio run`,
# so this harness drives it two ways:
#
#   * **the whole shim**, as cron does — which is now the only way to exercise
#     the pause gate, the lock, the sweeps, the listing, the Plex scan and the
#     summary line, because none of them is a bash function any more. It is
#     also a better test than calling functions ever was: it runs the thing
#     that actually runs.
#   * **`putio_python <subcommand>`**, sourced from the shim — the documented
#     hand-run door onto the same code, and the only way to drive one stage in
#     isolation.
#
# What stays bash is what is checked as bash: binary discovery, the config
# handoff, the mount gate, and the guards that stop a run starting wrong.
#
# Requires rclone and jq, which the processor requires anyway.

# Deliberately no `set -u`/`set -e`: the sourced shim runs under whatever
# options this shell has, and the processor runs under none of them. Testing it
# stricter than production invents failures that cannot happen there.
set -o pipefail

SELF_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_UNDER_TEST="${1:-$SELF_DIR/../putio_download_and_delete.sh}"
[ -f "$SCRIPT_UNDER_TEST" ] || { echo "No such script: $SCRIPT_UNDER_TEST" >&2; exit 1; }
SCRIPT_UNDER_TEST="$(cd -P "$(dirname "$SCRIPT_UNDER_TEST")" && pwd)/$(basename "$SCRIPT_UNDER_TEST")"
SCRIPT_DIR_UNDER_TEST="$(dirname "$SCRIPT_UNDER_TEST")"

for dep in rclone jq; do
    command -v "$dep" >/dev/null 2>&1 || { echo "Missing dependency: $dep" >&2; exit 1; }
done

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/putio_harness.XXXXXX")" || exit 1
trap 'rm -rf "$WORKDIR"' EXIT

PASSED=0
FAILED=0
ok()   { PASSED=$((PASSED + 1)); printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { FAILED=$((FAILED + 1)); printf '  \033[31m✗\033[0m %s\n' "$*"; }
check() { if [ "$1" = "0" ]; then ok "$2"; else bad "$2${3:+ — $3}"; fi; }
skip() { printf '  \033[33m-\033[0m %s\n' "$*"; }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Load the shim and sandbox it
#
# Sourcing runs everything above the sourced-guard, the config file included.
# So every path the processor writes to has to be overridden AFTER the source,
# never before — an exported LOG_FILE loses to the config's own assignment, and
# the run then appends to the live log.
#
# What sourcing buys since phase 5 is narrower than it was: `putio_python`,
# `PUTIO_PY_VARS`, the mount functions and the resolved binaries. The run
# itself is driven as a subprocess, below.
# ---------------------------------------------------------------------------

# shellcheck disable=SC1090
source "$SCRIPT_UNDER_TEST" || { echo "Failed to source $SCRIPT_UNDER_TEST" >&2; exit 1; }

REMOTE="$WORKDIR/remote"
LOCAL_DIR="$WORKDIR/local"
STAGING_DIR="$WORKDIR/staging"
PROGRESS_STATE_DIR="$WORKDIR/progress"
UNMATCHED_DIR="$WORKDIR/unmatched"
MOVIE_LIBRARY="$WORKDIR/movies"
TV_LIBRARY="$WORKDIR/tv"
LOG_FILE="$WORKDIR/harness.log"
LOCKFILE="$WORKDIR/harness.lock"
PAUSE_FILE="$WORKDIR/harness.paused"
MEDIA_MOUNT_POINT=""
MEDIA_MOUNT_STATE_DIR="$WORKDIR/mountstate"
DELETE_AFTER_DOWNLOAD="false"
MIN_FREE_SPACE_GB=1
MAX_RETRIES=1
RETRY_DELAY=1
RCLONE_BWLIMIT="2M"
RCLONE_STATS_INTERVAL="1s"
RCLONE_MAX_CONCURRENT=1
RATE_LIMIT_DELAY=0
LOG_LEVEL="INFO"
CRON_DEBUG="${CRON_DEBUG:-}"
TERM="${TERM:-dumb}"

# The one guard that matters, and it runs before a single byte is written —
# creating the sandbox is itself a write, and `: > "$LOG_FILE"` on a LOG_FILE
# that slipped through would truncate the live log before any check could stop
# it. Nothing above this line may create, truncate or delete anything.
for var in LOG_FILE LOCAL_DIR PROGRESS_STATE_DIR STAGING_DIR UNMATCHED_DIR \
           MOVIE_LIBRARY TV_LIBRARY REMOTE LOCKFILE PAUSE_FILE MEDIA_MOUNT_STATE_DIR; do
    case "${!var}" in
        "$WORKDIR"/*) ;;
        *) printf 'ABORT: %s=%s is outside the sandbox %s\n' "$var" "${!var}" "$WORKDIR" >&2; exit 1 ;;
    esac
done

mkdir -p "$REMOTE" "$LOCAL_DIR" "$STAGING_DIR" "$UNMATCHED_DIR" "$MOVIE_LIBRARY" "$TV_LIBRARY"
: > "$LOG_FILE"

# rclone clones local files instead of copying them, which finishes before any
# progress could be observed. This forces real byte movement so the bwlimit
# above actually paces the transfer.
export RCLONE_LOCAL_NO_CLONE=true

printf '\033[1mprogress harness\033[0m\n  script:  %s\n  sandbox: %s\n' "$SCRIPT_UNDER_TEST" "$WORKDIR"

HAVE_PY=1
[ -n "$PYTHON_BIN" ] && [ -d "$SCRIPT_DIR_UNDER_TEST/putio" ] && HAVE_PY=0
[ "$HAVE_PY" = "0" ] || printf '  \033[33m(no interpreter or no putio/ — most sections will skip)\033[0m\n'

mkfixture() { dd if=/dev/urandom of="$1" bs=1048576 count="${2:-2}" 2>/dev/null; }

meta_count() { ls -1 "$PROGRESS_STATE_DIR"/dl.*.meta 2>/dev/null | wc -l | tr -d ' '; }
state_count() { ls -A "$PROGRESS_STATE_DIR" 2>/dev/null | wc -l | tr -d ' '; }
staging_count() { ls -A "$STAGING_DIR" 2>/dev/null | wc -l | tr -d ' '; }
meta_field() { sed -n "s/^$2=//p" "$1"; }
logtail() { tail -60 "$LOG_FILE"; }

# Waits for a condition rather than sleeping a fixed amount: the suite stays
# fast on a quick disk and still passes on a slow one.
await() {
    local tries=0
    until eval "$1"; do
        tries=$((tries + 1))
        [ "$tries" -gt "${2:-100}" ] && return 1
        sleep 0.1
    done
    return 0
}

# ---------------------------------------------------------------------------
# A throwaway processor, with its own config
#
# `make_processor NAME` writes a copy of the shim and a config beside it, built
# from the sandbox's own paths plus whatever extra lines are on stdin. Running
# a *copy* rather than the original is what lets a check point PYTHON_BIN at a
# stub, or break a setting on purpose, without touching anything the next check
# reads.
# ---------------------------------------------------------------------------
make_processor() {
    local name="$1" dir="$WORKDIR/$1"
    mkdir -p "$dir"
    cp "$SCRIPT_UNDER_TEST" "$dir/putio_download_and_delete.sh"
    [ -d "$SCRIPT_DIR_UNDER_TEST/putio" ] && cp -R "$SCRIPT_DIR_UNDER_TEST/putio" "$dir/putio"
    {
        cat <<CFG
REMOTE="$REMOTE/"
LOCAL_DIR="$LOCAL_DIR"
STAGING_DIR="$STAGING_DIR"
USE_TEMP_DIR="true"
MOVIE_LIBRARY="$MOVIE_LIBRARY"
TV_LIBRARY="$TV_LIBRARY"
UNMATCHED_DIR="$UNMATCHED_DIR"
MEDIA_MOUNT_POINT=""
MEDIA_MOUNT_STATE_DIR="$WORKDIR/mountstate"
LOG_FILE="$LOG_FILE"
LOG_LEVEL="$LOG_LEVEL"
LOCKFILE="$LOCKFILE"
PAUSE_FILE="$PAUSE_FILE"
PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR"
RCLONE_STATS_INTERVAL="1s"
RCLONE_BWLIMIT="2M"
RCLONE_MAX_CONCURRENT=1
MIN_FREE_SPACE_GB=1
MAX_RETRIES=1
RETRY_DELAY=1
RATE_LIMIT_DELAY=0
DELETE_AFTER_DOWNLOAD="true"
PLEX_URL="http://127.0.0.1:9"
TMDB_API_KEY="harness-not-a-real-key"
PLEX_TOKEN="harness-plex-token"
CFG
        cat
    } > "$dir/.putio_config"
    printf '%s' "$dir"
}

# Runs a processor copy the way cron does, with TERM set so the [ENV] banner
# stays out of the way.
run_processor() {
    local dir="$1"; shift
    ( cd "$dir" && TERM=xterm PUTIO_WEBHOOK_DELAY=0 \
        bash "$dir/putio_download_and_delete.sh" "$@" </dev/null >/dev/null 2>&1 )
}

# ---------------------------------------------------------------------------
section "the shim: what is still bash, and why"
# ---------------------------------------------------------------------------

# rclone and jq are the two names whose resolution the PATH ordering actually
# moves, and both were silently different under cron than in a shell —
# transfers ran on a build nobody ever tested by hand.
[ -n "$RCLONE_CMD" ] && [ -x "$RCLONE_CMD" ]; check $? "RCLONE_CMD resolves to an executable" "got '$RCLONE_CMD'"
[ -n "$JQ_CMD" ] && [ -x "$JQ_CMD" ]; check $? "JQ_CMD resolves to an executable" "got '$JQ_CMD'"

# Every jq call site must go through the variable, or pinning it is a fiction.
# The shim has no jq call sites left, but it still discovers and hands over
# JQ_CMD — putioctl reads the same pin, so the two cannot parse one rclone log
# with two different jq builds.
STRAY=$(grep -nE "(^|[|(]|[[:space:]])jq[[:space:]]+[-'\"]" "$SCRIPT_UNDER_TEST" | grep -v 'JQ_CMD')
[ -z "$STRAY" ]; check $? "no jq call site bypasses JQ_CMD" "$(printf '%s' "$STRAY" | head -2 | tr '\n' ' ')"

case "$PATH" in
    /opt/homebrew/bin:*) BREW_FIRST=0 ;;
    *) BREW_FIRST=1 ;;
esac
[ "$BREW_FIRST" = "0" ]; check $? "the exported PATH puts Homebrew first"

# The shim must not have grown the processor back. These are the functions
# phase 5 deleted, and each one is a rule that now has exactly one
# implementation; a reappearance means two.
GREW=""
for fn in parse_media_info sanitize_filename is_junk_file rate_limit_tmdb \
          scan_plex_library get_plex_library_id identify_and_move_movie \
          identify_and_move_tv check_extraction_marker create_extraction_marker \
          check_rar_completeness cleanup_archive_files delete_multipart_rar_from_putio \
          download_files_parallel process_single_file download_file extract_archive \
          retry_command check_disk_space progress_begin; do
    grep -q "^${fn}()" "$SCRIPT_UNDER_TEST" && GREW="$GREW $fn"
done
[ -z "$GREW" ]; check $? "no deleted bash implementation has come back" "$GREW"

# And the shim is a shim. A line count is a blunt instrument, but it is the one
# that notices the whole processor being pasted back in.
SHIM_LINES=$(grep -c '' "$SCRIPT_UNDER_TEST")
[ "$SHIM_LINES" -lt 1400 ]; check $? "the shim is still a shim" "$SHIM_LINES lines"

# The exec is what keeps the pid. Every progress file is named after it, the
# lockfile holds it, and `putioctl stop` walks pgrep down from it.
grep -q '^exec env ' "$SCRIPT_UNDER_TEST"
check $? "the handover is an exec, so the run keeps cron's pid"

# ---------------------------------------------------------------------------
section "the Python handoff"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter or no putio/; skipping the handoff checks"
else
    [ -x "$PYTHON_BIN" ]; check $? "PYTHON_BIN resolves to an executable" "got '$PYTHON_BIN'"

    # The trap this pin exists for. `which python3` under the processor's own
    # PATH still lands on Apple's 3.9.6, because the versioned Homebrew formula
    # installs no python3 symlink at all — so a pin that merely echoes what
    # PATH would have found has pinned nothing.
    case "$PYTHON_BIN" in /*) ABS=0 ;; *) ABS=1 ;; esac
    [ "$ABS" = "0" ]; check $? "PYTHON_BIN is an absolute path" "got '$PYTHON_BIN'"

    PYVER=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    case "$PYVER" in 3.9|3.10|3.11|3.12|3.13|3.14) PYOK=0 ;; *) PYOK=1 ;; esac
    [ "$PYOK" = "0" ]; check $? "PYTHON_BIN is a supported Python 3" "got '$PYVER'"

    # The package has to import from an arbitrary working directory, because
    # that is what cron gives the processor.
    (cd / && PYTHONPATH="$SCRIPT_DIR_UNDER_TEST" "$PYTHON_BIN" -B -c 'import putio' 2>/dev/null)
    check $? "putio/ imports from an unrelated working directory"

    # putio_python must reach the package beside the script under test, not one
    # that happens to sit in the harness's working directory. Comparing
    # __file__ is the only way to tell those apart.
    LOADED=$(putio_python version 2>/dev/null | "$JQ_CMD" -r '.package')
    WANT=$(cd "$SCRIPT_DIR_UNDER_TEST" && pwd -P)/putio
    [ "$LOADED" = "$WANT" ]; check $? "putio_python loads the package beside the script" "got '$LOADED'"

    # The suite ships with the package so it can be run against a deployed
    # tree, not just the checkout.
    (cd "$SCRIPT_DIR_UNDER_TEST" && "$PYTHON_BIN" -B -m unittest discover >/dev/null 2>&1)
    check $? "the unittest suite passes under PYTHON_BIN"

    SELFTEST=$(putio_python selftest -q 2>&1)
    check $? "putio_python hands the environment over" "$(printf '%s' "$SELFTEST" | head -2 | tr '\n' ' ')"

    # The contract: PUTIO_PY_VARS and putio/config.py must name the same set. A
    # name added on one side only arrives as an empty string, which is
    # indistinguishable from "not configured" and so fails silently.
    PY_NAMES=$(cd "$SCRIPT_DIR_UNDER_TEST" && "$PYTHON_BIN" -B -c \
        'from putio.config import Config
for n in Config.env_names(): print(n)' 2>/dev/null | sort)
    SH_NAMES=$(printf '%s\n' $PUTIO_PY_VARS | sort)
    DRIFT=$(diff <(printf '%s\n' "$SH_NAMES") <(printf '%s\n' "$PY_NAMES") 2>/dev/null)
    [ -z "$DRIFT" ]; check $? "PUTIO_PY_VARS matches putio/config.py" \
        "$(printf '%s' "$DRIFT" | tr '\n' ' ')"

    # Secrets are in PUTIO_PY_VARS, so they reach the Python process — but they
    # must not come back out of it. The fake keys stay inside the command
    # substitution's subshell: an assignment prefixing a *function* call
    # persists after it in bash, so without the subshell these would quietly
    # replace the real values for every check that follows.
    DUMP=$(TMDB_API_KEY="harness-tmdb-secret" PLEX_TOKEN="harness-plex-secret" \
           PLEX_REQUEST_WEBHOOK_KEY="harness-hook-secret" \
           putio_python config-dump 2>&1)
    printf '%s' "$DUMP" | grep -q "harness-.*-secret"
    [ $? -ne 0 ]; check $? "config-dump redacts the API keys"

    # And the reason the values are passed on the Python process's own
    # environment rather than exported: rclone, curl and unrar have no business
    # inheriting the API keys.
    LEAKED=$(env | grep -E '^(TMDB_API_KEY|PLEX_TOKEN|PLEX_REQUEST_WEBHOOK_KEY|DROVIE_WEBHOOK_KEY)=' \
                 | cut -d= -f1 | tr '\n' ' ')
    [ -z "$LEAKED" ]; check $? "secrets are not exported to every child"

    # CONFIG_FILE joined the handoff in phase 5, and it is the answer to "which
    # config did this run actually read" — a deployed run and a hand-run one
    # can legitimately be reading two different files.
    DUMPED_CFG=$(putio_python config-dump --json 2>/dev/null | "$JQ_CMD" -r '.config_file')
    [ "$DUMPED_CFG" = "$CONFIG_FILE" ]
    check $? "config-dump names the config the shim sourced" "got '$DUMPED_CFG'"
fi

# ---------------------------------------------------------------------------
section "the guards that stop a run starting wrong"
# ---------------------------------------------------------------------------

# A pin that is set and wrong must be fatal at startup, not at first use. This
# is the failure RCLONE_CMD once had: every download logging "Could not
# determine size", with nothing at startup naming the cause.
PIN=$(make_processor pin <<'PINCFG'
PYTHON_BIN="/nonexistent/python3"
PINCFG
)
BADPIN=$(cd "$PIN" && bash "$PIN/putio_download_and_delete.sh" 2>&1 </dev/null)
BADPIN_RC=$?
{ [ "$BADPIN_RC" != "0" ] && printf '%s' "$BADPIN" | grep -q "PYTHON_BIN is set to"; }
check $? "a mistyped PYTHON_BIN pin is fatal at startup" "rc=$BADPIN_RC: $(printf '%s' "$BADPIN" | head -1)"

# ...and since phase 5, so is an absent one. Through phases 1-4 every Python
# call site had a bash fallback behind it, so a missing interpreter cost the
# features that needed it and the sync carried on. Those fallbacks are gone.
NOPY="$WORKDIR/nopy"
mkdir -p "$NOPY"
sed -e 's|/opt/homebrew/bin/python3\.1[123]|/opt/homebrew/bin/nope|g' \
    -e 's|/usr/local/bin/python3\.1[123]|/usr/local/bin/nope|g' \
    -e 's|/usr/bin/python3;|/usr/bin/nope3;|' \
    "$SCRIPT_UNDER_TEST" > "$NOPY/putio_download_and_delete.sh"
cp "$PIN/.putio_config" "$NOPY/.putio_config"
sed -i.bak 's|^PYTHON_BIN=.*|PYTHON_BIN=""|' "$NOPY/.putio_config"
NOPIN=$(cd "$NOPY" && env -u PYTHON_BIN bash "$NOPY/putio_download_and_delete.sh" 2>&1 </dev/null)
NOPIN_RC=$?
{ [ "$NOPIN_RC" != "0" ] && printf '%s' "$NOPIN" | grep -q "no Python interpreter found"; }
check $? "no interpreter at all is fatal too, and says how to fix it" \
    "rc=$NOPIN_RC: $(printf '%s' "$NOPIN" | head -1)"

# The config errors reach the log as well as stdout. Bash echoed them and
# nothing else, so a run that refused to start over a missing key left nothing
# in the log saying why.
GUARD=$(make_processor guard <<'GUARDCFG'
TMDB_API_KEY=""
GUARDCFG
)
sed -i.bak "s|^LOG_FILE=.*|LOG_FILE=\"$WORKDIR/guard.log\"|" "$GUARD/.putio_config"
GUARD_OUT=$(cd "$GUARD" && TERM=xterm bash "$GUARD/putio_download_and_delete.sh" 2>&1 </dev/null)
GUARD_RC=$?
[ "$GUARD_RC" != "0" ]; check $? "a config with no TMDB_API_KEY refuses to run" "rc=$GUARD_RC"
printf '%s' "$GUARD_OUT" | grep -q "TMDB_API_KEY not set"
check $? "...and says so on stdout, which is what cron mails"
grep -q "TMDB_API_KEY not set" "$WORKDIR/guard.log" 2>/dev/null
check $? "...and in the log, which bash never did"

# A malformed RCLONE_STATS_INTERVAL makes rclone exit at flag-parse time on
# every single download — before --log-file opens, so with no cause logged at
# all. This knob only drives the bar, so it must fall back rather than take the
# transfers with it.
STATS=$(make_processor stats <<'STATSCFG'
RCLONE_STATS_INTERVAL="5sec"
STATSCFG
)
sed -i.bak "s|^LOG_FILE=.*|LOG_FILE=\"$WORKDIR/stats.log\"|" "$STATS/.putio_config"
rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
mkfixture "$REMOTE/stats-no-year.mkv" 2
run_processor "$STATS"
grep -q "RCLONE_STATS_INTERVAL='5sec' is not a duration — using 5s" "$WORKDIR/stats.log"
check $? "a malformed RCLONE_STATS_INTERVAL warns and falls back to 5s"
# ...and a transfer actually happens, which is the half that matters and the
# half this check used to miss. The corrected value has to reach the rclone
# command line: rclone rejects a bad --stats at flag-parse time, *before*
# --log-file opens, so a fallback that only logged would leave every download
# in the run failing with no cause recorded anywhere.
[ -f "$UNMATCHED_DIR/stats-no-year.mkv" ]
check $? "...and the corrected value reaches rclone, so the download succeeds"
rm -f "$REMOTE"/* "$UNMATCHED_DIR"/* 2>/dev/null

# ---------------------------------------------------------------------------
section "the mount gate"
# ---------------------------------------------------------------------------

# putio/mount.py re-enters the processor once per file, because SMB recovery is
# the one thing the plan says never to port. It must answer and exit without
# running any of the run body — a --mount-gate that took the lockfile would
# deadlock the run that called it.
GATE=$(make_processor gate <<GATECFG
LOCKFILE="$WORKDIR/gate.lock"
PAUSE_FILE="$WORKDIR/gate.paused"
LOG_FILE="$WORKDIR/gate.log"
GATECFG
)
run_processor "$GATE" --mount-gate
check $? "--mount-gate exits 0 when the mount feature is off"
[ ! -e "$WORKDIR/gate.lock" ]; check $? "--mount-gate never touches the lockfile"

# ...and not the pause gate either. A paused sync must still be able to answer
# "is the share up?" for anything that asks.
printf 'PAUSED_UNTIL=0\n' > "$WORKDIR/gate.paused"
run_processor "$GATE" --mount-gate
check $? "--mount-gate answers even while the sync is paused"
rm -f "$WORKDIR/gate.paused"

sed -i.bak "s|^MEDIA_MOUNT_POINT=.*|MEDIA_MOUNT_POINT=\"$WORKDIR/no-such-share\"|" \
    "$GATE/.putio_config"
run_processor "$GATE" --mount-gate
GATE_RC=$?
[ "$GATE_RC" -ne 0 ]; check $? "--mount-gate reports a share that is gone" "rc=$GATE_RC"

# And the *preflight*, which is the same gate asked once at startup rather than
# once per file. Every other section here runs with MEDIA_MOUNT_POINT empty, so
# this branch is never otherwise reached — and it shipped broken once for
# exactly that reason, calling a method MountGate does not have. The first real
# run after deploying found it; this is so the next one does not have to.
#
# "/" rather than a share: it is genuinely in the mount table and `stat -f`
# answers instantly, so the alive path is exercised without going near the NAS.
: > "$WORKDIR/gate.log"
ALIVE=$(make_processor mountalive <<ALIVECFG
MEDIA_MOUNT_POINT="/"
LOG_FILE="$WORKDIR/gate.log"
LOCKFILE="$WORKDIR/gate.lock"
PAUSE_FILE="$WORKDIR/gate.paused"
ALIVECFG
)
run_processor "$ALIVE"
check $? "a run whose share is alive gets past the preflight"
grep -q "Script completed successfully" "$WORKDIR/gate.log"
check $? "...and completes"

: > "$WORKDIR/gate.log"
GONE=$(make_processor mountgone <<GONECFG
MEDIA_MOUNT_POINT="$WORKDIR/no-such-share"
LOG_FILE="$WORKDIR/gate.log"
LOCKFILE="$WORKDIR/gate.lock"
PAUSE_FILE="$WORKDIR/gate.paused"
GONECFG
)
GONE_OUT=$(cd "$GONE" && TERM=xterm bash "$GONE/putio_download_and_delete.sh" 2>&1 </dev/null)
GONE_RC=$?
[ "$GONE_RC" != "0" ]; check $? "a run whose share is gone refuses to start" "rc=$GONE_RC"
printf '%s' "$GONE_OUT" | grep -q "is not available and could not be remounted"
check $? "...and names the mount rather than failing somewhere downstream"

# ---------------------------------------------------------------------------
section "an idle run"
# ---------------------------------------------------------------------------

# cron fires this every few minutes and most runs find nothing to do, so an
# idle run costs exactly one INFO line. rotate_log once cost 36s of every one
# of them by putting subprocesses in a per-line loop; the same mistake is
# available in Python, so the duration is measured rather than assumed.
IDLE=$(make_processor idle <<IDLECFG
LOG_FILE="$WORKDIR/idle.log"
IDLECFG
)
: > "$WORKDIR/idle.log"
IDLE_START=$(date +%s)
run_processor "$IDLE"
check $? "an idle run exits 0"
IDLE_ELAPSED=$(( $(date +%s) - IDLE_START ))
[ "$(grep -c '' "$WORKDIR/idle.log")" = "1" ]
check $? "an idle run costs exactly one log line" "$(grep -c '' "$WORKDIR/idle.log") lines"
grep -q "=== Script completed successfully — nothing to do (" "$WORKDIR/idle.log"
check $? "the completion phrase is verbatim — putioctl status greps for it"
[ "$IDLE_ELAPSED" -le 5 ]; check $? "an idle run stays fast" "took ${IDLE_ELAPSED}s"

# ...and it costs one line **under cron**, which is the case that actually
# happens every five minutes and the one `run_processor` cannot reproduce,
# because it sets TERM.
#
# cron sets no TERM at all, and bash then sets TERM=dumb itself — as an
# ordinary shell variable, *not* an exported one. So `[ -z "$TERM" ]` is false
# inside the shim while os.environ.get("TERM") is None in the child, and the
# [ENV] banner that used to appear only under CRON_DEBUG started printing on
# every scheduled run: nine lines instead of one, every five minutes, for the
# life of the log. The shim hands its own TERM over to close the gap.
: > "$WORKDIR/idle.log"
( cd "$IDLE" && env -u TERM -u CRON_DEBUG bash "$IDLE/putio_download_and_delete.sh" \
    </dev/null >/dev/null 2>&1 )
check $? "a cron-shaped run (no TERM) exits 0"
[ "$(grep -c '' "$WORKDIR/idle.log")" = "1" ]
check $? "an idle run under cron also costs exactly one line" \
    "$(grep -c '' "$WORKDIR/idle.log") lines: $(head -1 "$WORKDIR/idle.log" | cut -c1-60)"

# CRON_DEBUG is what turns the banner back on, and it must still work.
: > "$WORKDIR/idle.log"
( cd "$IDLE" && env -u TERM CRON_DEBUG=1 bash "$IDLE/putio_download_and_delete.sh" \
    </dev/null >/dev/null 2>&1 )
grep -q "\[ENV\] Running in cron/non-interactive mode" "$WORKDIR/idle.log"
check $? "CRON_DEBUG=1 still asks for the environment banner"

# A log with a day of history in it must not slow that down, which is what the
# string comparison in rotate is for.
: > "$WORKDIR/idle.log"
# Half the lines from two days ago and half from the last hour, so rotation
# has something to drop *and* something to keep.
"$PYTHON_BIN" - "$WORKDIR/idle.log" <<'PY' 2>/dev/null
import sys, time
now = time.time()
with open(sys.argv[1], "w") as handle:
    for n in range(10000):
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 172800 - n))
        handle.write("[{0}] [INFO] ancient {1}\n".format(old, n))
    for n in range(10000):
        new = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600 + n // 10))
        handle.write("[{0}] [INFO] recent {1}\n".format(new, n))
PY
ROT_START=$(date +%s)
run_processor "$IDLE"
ROT_ELAPSED=$(( $(date +%s) - ROT_START ))
[ "$ROT_ELAPSED" -le 10 ]; check $? "a 20k-line log rotates without per-line subprocesses" "took ${ROT_ELAPSED}s"
grep -q 'ancient' "$WORKDIR/idle.log"
[ $? -ne 0 ]; check $? "rotation dropped everything older than 24 hours"
grep -q 'recent 9999' "$WORKDIR/idle.log"; check $? "...and kept everything newer"

# ---------------------------------------------------------------------------
section "the pause gate"
# ---------------------------------------------------------------------------

PAUSED=$(make_processor paused <<PAUSECFG
LOG_FILE="$WORKDIR/paused.log"
LOCKFILE="$WORKDIR/paused.lock"
PAUSE_FILE="$WORKDIR/paused.file"
PAUSECFG
)
: > "$WORKDIR/paused.log"
printf 'PAUSED_UNTIL=0\nPAUSED_REASON="harness"\n' > "$WORKDIR/paused.file"
run_processor "$PAUSED"
check $? "a paused run exits 0 — cron mails failures, and this is not one"
grep -q "\[PAUSE\] Sync is paused indefinitely (harness) — skipping run" "$WORKDIR/paused.log"
check $? "the pause is noted with its reason"
[ ! -e "$WORKDIR/paused.lock" ]; check $? "a paused run leaves no lockfile"

# Once per pause, not once per tick: rotation only runs on runs that proceed,
# so a line every five minutes would grow the log for the length of the pause
# with nothing ever trimming it.
run_processor "$PAUSED"
[ "$(grep -c 'Sync is paused' "$WORKDIR/paused.log")" = "1" ]
check $? "the next tick stops silently"

# A timed pause clears itself, so the schedule resumes without anyone having to
# remember `putioctl resume`.
: > "$WORKDIR/paused.log"
printf 'PAUSED_UNTIL=%s\n' "$(( $(date +%s) - 10 ))" > "$WORKDIR/paused.file"
run_processor "$PAUSED"
[ ! -e "$WORKDIR/paused.file" ]; check $? "an expired pause clears itself"
grep -q "\[PAUSE\] Pause expired" "$WORKDIR/paused.log"; check $? "...and says so"
grep -q "Script completed successfully" "$WORKDIR/paused.log"; check $? "...and the run proceeds"

# PUTIO_IGNORE_PAUSE=1 is what `putioctl sync --force` sets, and the pause has
# to survive the forced run.
: > "$WORKDIR/paused.log"
printf 'PAUSED_UNTIL=0\n' > "$WORKDIR/paused.file"
( cd "$PAUSED" && TERM=xterm PUTIO_IGNORE_PAUSE=1 \
    bash "$PAUSED/putio_download_and_delete.sh" </dev/null >/dev/null 2>&1 )
grep -q "Script completed successfully" "$WORKDIR/paused.log"
check $? "PUTIO_IGNORE_PAUSE=1 runs anyway"
[ -f "$WORKDIR/paused.file" ]; check $? "...and leaves the pause in force"
rm -f "$WORKDIR/paused.file"

# ---------------------------------------------------------------------------
section "the lock"
# ---------------------------------------------------------------------------

LOCKED=$(make_processor locked <<LOCKCFG
LOG_FILE="$WORKDIR/locked.log"
LOCKFILE="$WORKDIR/locked.lock"
PAUSE_FILE="$WORKDIR/locked.paused"
LOCKCFG
)
: > "$WORKDIR/locked.log"
printf '%s\n' "$$" > "$WORKDIR/locked.lock"
run_processor "$LOCKED"
check $? "a held lock is not a failure — cron overlapping a long run is the schedule working"
grep -q "\[INFO\] \[LOCK\] A sync is already running (PID $$" "$WORKDIR/locked.log"
check $? "...and names the holder at INFO, which is what putioctl status reports"
grep -q "elapsed .*) — skipping this run" "$WORKDIR/locked.log"
check $? "...with how long it has been going, the only sign of a wedged run"
grep -q "\[ERROR\]" "$WORKDIR/locked.log"
[ $? -ne 0 ]; check $? "...and nothing red in putioctl logs --errors"
[ -f "$WORKDIR/locked.lock" ]; check $? "...and the holder's lock is left alone"
# The exit status no longer says the tick stopped, so this is what does. Without
# it a run that logged the line and then went on to sync anyway passes every
# check above — two concurrent syncs, which is the one thing the lock exists to
# prevent.
grep -q "Script completed successfully" "$WORKDIR/locked.log"
[ $? -ne 0 ]; check $? "...and the tick really stopped rather than syncing anyway"

: > "$WORKDIR/locked.log"
printf '2147483646\n' > "$WORKDIR/locked.lock"
run_processor "$LOCKED"
check $? "a stale lock is broken"
grep -q "\[LOCK\] Stale lock found" "$WORKDIR/locked.log"; check $? "...and says so at WARN"
[ ! -e "$WORKDIR/locked.lock" ]; check $? "the lock is released when the run finishes"

# The lockfile holds the pid cron started, because the shim execs rather than
# forking. Everything outside this package identifies the run by that number.
: > "$WORKDIR/locked.log"
mkfixture "$REMOTE/Lock.Check.2001.1080p.mkv" 12
# `exec` in the subshell, or $! is the subshell's pid and the shim's is one
# higher — which is exactly the confusion the shim's own exec exists to avoid.
( cd "$LOCKED" && exec env TERM=xterm bash "$LOCKED/putio_download_and_delete.sh" \
    </dev/null >/dev/null 2>&1 ) &
SHIM_PID=$!
if await '[ -s "'"$WORKDIR"'/locked.lock" ]' 100; then
    HELD=$(cat "$WORKDIR/locked.lock" 2>/dev/null | tr -d ' \n')
    [ "$HELD" = "$SHIM_PID" ]
    check $? "the lock holds the pid cron started — exec keeps it" "held $HELD, started $SHIM_PID"
    # And the progress files are named after the same number, which is what
    # lets a reader tell a live transfer from what a killed run left behind.
    await '[ "$(meta_count)" -ge 1 ]' 100
    ls -1 "$PROGRESS_STATE_DIR"/dl."$SHIM_PID".*.meta >/dev/null 2>&1
    check $? "progress files are named after that same pid"
else
    bad "the run never took the lock"
fi
# Children first: once the parent dies its children are reparented and
# `pgrep -P` can no longer find them, which is how a killed run leaves orphaned
# rclone transfers behind. `putioctl stop` collects the tree up front for the
# same reason.
for p in $(pgrep -P "$SHIM_PID" 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
kill -TERM "$SHIM_PID" 2>/dev/null
wait "$SHIM_PID" 2>/dev/null
STOP_RC=$?
# The lock and the bars are cleared by the run's own `finally`, which needs a
# moment after the signal lands.
await '[ ! -e "'"$WORKDIR"'/locked.lock" ]' 100

# SIGTERM is what `putioctl stop` sends. Bash's EXIT trap did not reliably fire
# on it, so a stopped run left its lockfile and its bars behind for the next
# run's sweeps to find. The sweeps stay — SIGKILL still exists — but the
# ordinary stop is clean now.
[ "$STOP_RC" = "143" ]; check $? "a stopped run reports being signalled" "rc=$STOP_RC"
[ ! -e "$WORKDIR/locked.lock" ]; check $? "SIGTERM releases the lock"
[ "$(state_count)" = "0" ]; check $? "SIGTERM clears the bars it published"
[ "$(staging_count)" = "0" ]; check $? "SIGTERM clears its staging directory"
[ -e "$REMOTE/Lock.Check.2001.1080p.mkv" ]
check $? "put.io keeps the copy of a transfer that was interrupted"
grep -q "\[RUN\] Exited without completing" "$WORKDIR/locked.log"
check $? "an incomplete run leaves an INFO-level trace"
rm -f "$REMOTE"/*.mkv

# ---------------------------------------------------------------------------
section "progress files, without a transfer"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the progress-protocol checks"
else
    rm -f "$PROGRESS_STATE_DIR"/dl.* 2>/dev/null
    # Driven through the package rather than through a transfer, so the file
    # format can be checked without waiting on rclone. Three readers parse
    # these — putioctl, the curses console and putio-statusd — so the format is
    # the test.
    PKG_PARENT="$SCRIPT_DIR_UNDER_TEST" PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" \
    LOCAL_DIR="$LOCAL_DIR" RUN_PID="$$" "$PYTHON_BIN" -B -c '
import os, sys
sys.path.insert(0, os.environ["PKG_PARENT"])
from putio.progress import ProgressPublisher
pub = ProgressPublisher(os.environ["PROGRESS_STATE_DIR"], os.environ["RUN_PID"])
job = pub.begin("/Fixture.Name.mkv", 1234, os.environ["LOCAL_DIR"] + "/Fixture.Name.mkv")
print(job.meta_path)
job.phase("copy", os.environ["LOCAL_DIR"] + "/Fixture.Name.mkv")
' > "$WORKDIR/meta-path" 2>"$WORKDIR/meta-err"
    META=$(cat "$WORKDIR/meta-path" 2>/dev/null)
    if [ -z "$META" ] || [ ! -f "$META" ]; then
        bad "progress publishing failed: $(head -3 "$WORKDIR/meta-err" | tr '\n' ' ')"
    else
        [ "$(meta_field "$META" NAME)" = "Fixture.Name.mkv" ]; check $? "meta carries the file name"
        [ "$(meta_field "$META" SIZE)" = "1234" ]; check $? "meta carries the size"
        [ "$(meta_field "$META" RUN_PID)" = "$$" ]; check $? "meta is stamped with the run pid"
        [ "$(meta_field "$META" PHASE)" = "copy" ]; check $? "the phase switches to copy for the staged copy"
        [ -f "$(meta_field "$META" LOG)" ]; check $? "the rclone log is opened up front"
    fi
    rm -f "$PROGRESS_STATE_DIR"/dl.*
fi

# ---------------------------------------------------------------------------
section "sweeping what a killed run left behind"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the sweep checks"
else
    mkdir -p "$PROGRESS_STATE_DIR"
    sweep_now() {
        PKG_PARENT="$SCRIPT_DIR_UNDER_TEST" PROGRESS="$PROGRESS_STATE_DIR" \
        STAGING="$STAGING_DIR" "$PYTHON_BIN" -B -c '
import os, sys
sys.path.insert(0, os.environ["PKG_PARENT"])
from putio import sweep
sweep.sweep_progress(os.environ["PROGRESS"])
sweep.sweep_staging(os.environ["STAGING"], lambda level, message: None)
' 2>/dev/null
    }
    BOOT=$(PKG_PARENT="$SCRIPT_DIR_UNDER_TEST" "$PYTHON_BIN" -B -c '
import os, sys
sys.path.insert(0, os.environ["PKG_PARENT"])
from putio import sweep
print(sweep.boot_epoch())' 2>/dev/null)
    [ -n "$BOOT" ] && [ "$BOOT" -gt 0 ] 2>/dev/null
    check $? "boot time resolves to a usable epoch" "got '$BOOT'"

    DEAD_PID=2147483646
    printf 'RUN_PID=%s\n' "$DEAD_PID" > "$PROGRESS_STATE_DIR/dl.$DEAD_PID.1.meta"
    printf 'RUN_PID=%s\n' "$$" > "$PROGRESS_STATE_DIR/dl.$$.1.meta"
    sweep_now
    [ ! -f "$PROGRESS_STATE_DIR/dl.$DEAD_PID.1.meta" ]; check $? "the sweep drops a dead run's files"
    [ -f "$PROGRESS_STATE_DIR/dl.$$.1.meta" ]; check $? "the sweep keeps a live run's files"
    rm -f "$PROGRESS_STATE_DIR"/dl.*

    # A pid only identifies a run within the boot that issued it. After a
    # restart the kernel hands the same numbers out again, so `kill -0` alone
    # answers "alive" for an unrelated process and the entry is skipped by its
    # sweep for good. $$ is used as the pid on purpose: it is unambiguously
    # live, so only the mtime can decide these.
    PRE_BOOT=$(date -r "$(( BOOT - 60 ))" '+%Y%m%d%H%M.%S' 2>/dev/null || date -d "@$(( BOOT - 60 ))" '+%Y%m%d%H%M.%S')
    POST_BOOT=$(date -r "$(( BOOT + 60 ))" '+%Y%m%d%H%M.%S' 2>/dev/null || date -d "@$(( BOOT + 60 ))" '+%Y%m%d%H%M.%S')

    printf 'RUN_PID=%s\n' "$$" > "$PROGRESS_STATE_DIR/dl.$$.2.meta"
    touch -t "$PRE_BOOT" "$PROGRESS_STATE_DIR/dl.$$.2.meta"
    sweep_now
    [ ! -f "$PROGRESS_STATE_DIR/dl.$$.2.meta" ]
    check $? "the sweep drops a live pid's file left from before boot"

    printf 'RUN_PID=%s\n' "$$" > "$PROGRESS_STATE_DIR/dl.$$.3.meta"
    touch -t "$POST_BOOT" "$PROGRESS_STATE_DIR/dl.$$.3.meta"
    sweep_now
    [ -f "$PROGRESS_STATE_DIR/dl.$$.3.meta" ]
    check $? "the sweep keeps a live pid's file written since boot"
    rm -f "$PROGRESS_STATE_DIR"/dl.*

    # Staging is the one that costs real space: a full-size media file in
    # exactly the space check_disk_space measures.
    STALE_STAGE="$STAGING_DIR/dl_${$}_preboot"
    mkdir -p "$STALE_STAGE" && dd if=/dev/zero of="$STALE_STAGE/part.mkv" bs=1k count=8 2>/dev/null
    touch -t "$PRE_BOOT" "$STALE_STAGE"
    sweep_now
    [ ! -d "$STALE_STAGE" ]; check $? "the sweep reclaims a live pid's staging dir from before boot"

    LIVE_STAGE="$STAGING_DIR/dl_${$}_postboot"
    mkdir -p "$LIVE_STAGE"
    touch -t "$POST_BOOT" "$LIVE_STAGE"
    sweep_now
    [ -d "$LIVE_STAGE" ]; check $? "the sweep keeps an in-flight transfer's directory"
    rm -rf "$STAGING_DIR"/dl_*

    # And the whole set runs as part of a real run's startup, not just when
    # called by hand.
    : > "$LOG_FILE"
    STALE_STAGE="$STAGING_DIR/dl_${DEAD_PID}_gone"
    mkdir -p "$STALE_STAGE" && dd if=/dev/zero of="$STALE_STAGE/part.mkv" bs=1k count=8 2>/dev/null
    printf 'RUN_PID=%s\n' "$DEAD_PID" > "$PROGRESS_STATE_DIR/dl.$DEAD_PID.9.meta"
    SWEEPER=$(make_processor sweeper <<'SWEEPCFG'
SWEEPCFG
)
    run_processor "$SWEEPER"
    [ ! -d "$STALE_STAGE" ] && [ ! -f "$PROGRESS_STATE_DIR/dl.$DEAD_PID.9.meta" ]
    check $? "a real run sweeps both at startup"
    grep -q "\[CLEANUP\] Removed 1 abandoned staging dir" "$LOG_FILE"
    check $? "...and says what it reclaimed"
fi

# ---------------------------------------------------------------------------
section "a staged download, start to finish"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the transfer checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    # A name that does not parse: it goes to Unmatched without a TMDb lookup,
    # so the whole transfer path is exercised offline.
    mkfixture "$REMOTE/no-year-here.mkv" 12
    XFER=$(make_processor xfer <<'XFERCFG'
XFERCFG
)
    ( cd "$XFER" && TERM=xterm bash "$XFER/putio_download_and_delete.sh" </dev/null >/dev/null 2>&1 ) &
    DL=$!
    if await '[ "$(meta_count)" = "1" ]' 150; then
        LIVE=$(ls -1 "$PROGRESS_STATE_DIR"/dl.*.meta 2>/dev/null | head -1)
        [ "$(meta_field "$LIVE" NAME)" = "no-year-here.mkv" ]
        check $? "the transfer is published while it runs"
        await '[ -s "$(sed -n "s/^LOG=//p" "'"$LIVE"'")" ]' 100 && \
            jq -e '.stats.bytes >= 0' < <(grep '"stats"' "$(meta_field "$LIVE" LOG)" | tail -1) >/dev/null 2>&1
        check $? "rclone writes parseable JSON stats into the progress log"
        [ "$(staging_count)" -ge 1 ]; check $? "the transfer stages before it lands"
    else
        bad "no meta file appeared within 15s"
    fi
    wait $DL; DL_RC=$?
    [ "$DL_RC" = "0" ]; check $? "the run succeeds" "rc=$DL_RC"
    [ ! -e "$REMOTE/no-year-here.mkv" ]
    check $? "put.io releases a copy whose local file left staging"
    [ "$(state_count)" = "0" ]; check $? "no progress files outlive the transfer"
    [ "$(staging_count)" = "0" ]; check $? "no staging directory outlives the transfer"
    grep -q "\[DOWNLOAD\] Completed: no-year-here.mkv" "$LOG_FILE"
    check $? "the completion is logged"
    grep -q "=== Script completed successfully — 1 downloaded" "$LOG_FILE"
    check $? "the summary counts what landed"
fi

# ---------------------------------------------------------------------------
section "two downloads at once"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the concurrency checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    mkfixture "$REMOTE/alpha-no-year.mkv" 10
    mkfixture "$REMOTE/beta-no-year.mkv" 10
    cp "$REMOTE/alpha-no-year.mkv" "$WORKDIR/alpha.orig"
    cp "$REMOTE/beta-no-year.mkv" "$WORKDIR/beta.orig"
    PAIR=$(make_processor pair <<'PAIRCFG'
RCLONE_MAX_CONCURRENT=2
PAIRCFG
)
    ( cd "$PAIR" && TERM=xterm bash "$PAIR/putio_download_and_delete.sh" </dev/null >/dev/null 2>&1 ) &
    DL=$!
    if await '[ "$(meta_count)" = "2" ]' 150; then
        ok "each job publishes its own progress files"
        STAGING_DIRS=$(ls -1d "$STAGING_DIR"/*/ 2>/dev/null | wc -l | tr -d ' ')
        [ "$STAGING_DIRS" = "2" ]
        check $? "each job gets its own staging directory" "saw $STAGING_DIRS"
    else
        bad "both transfers did not appear within 15s"
    fi
    wait $DL
    check $? "both downloads succeed"
    cmp -s "$WORKDIR/alpha.orig" "$UNMATCHED_DIR/alpha-no-year.mkv" && \
        cmp -s "$WORKDIR/beta.orig" "$UNMATCHED_DIR/beta-no-year.mkv"
    check $? "neither job corrupted the other's file"
    [ "$(state_count)" = "0" ] && [ "$(staging_count)" = "0" ]
    check $? "nothing is left behind"
    grep -q "=== Script completed successfully — 2 downloaded" "$LOG_FILE"
    check $? "both are counted in the summary"
fi

# ---------------------------------------------------------------------------
section "a download that fails"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the failure checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    mkfixture "$REMOTE/blocked-no-year.mkv" 2
    # A read-only LOCAL_DIR on the *direct* path, so the failure is rclone's
    # own and the message it produced is what has to survive into the log.
    # (An unwritable STAGING_DIR would not do: validate_config refuses to start
    # the run at all, which is its job and a different test.)
    BLOCKED="$WORKDIR/blocked-local"
    mkdir -p "$BLOCKED" && chmod 500 "$BLOCKED"
    FAIL=$(make_processor fail <<FAILCFG
LOCAL_DIR="$BLOCKED"
USE_TEMP_DIR="false"
FAILCFG
)
    run_processor "$FAIL"
    grep -q "Failed to download blocked-no-year.mkv" "$LOG_FILE"
    check $? "the failure is logged"
    grep -q "Failed to download blocked-no-year.mkv.*—" "$LOG_FILE"
    check $? "the failure line carries rclone's own message" \
        "$(grep -o 'Failed to download.*' "$LOG_FILE" | head -1 | cut -c1-80)"
    [ -e "$REMOTE/blocked-no-year.mkv" ]
    check $? "put.io keeps the copy of a file that never landed"
    [ "$(state_count)" = "0" ]; check $? "a failed transfer cleans up its progress files"
    grep -q "nothing downloaded (1 file(s) on put.io)" "$LOG_FILE"
    check $? "the summary says files were offered and none arrived"
    chmod 700 "$BLOCKED"
fi

# ---------------------------------------------------------------------------
section "a direct download (USE_TEMP_DIR=false)"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the direct-path checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    mkfixture "$REMOTE/direct-no-year.mkv" 4
    cp "$REMOTE/direct-no-year.mkv" "$WORKDIR/direct.orig"
    DIRECT=$(make_processor direct <<'DIRECTCFG'
USE_TEMP_DIR="false"
DIRECTCFG
)
    run_processor "$DIRECT"
    check $? "the direct path succeeds"
    cmp -s "$WORKDIR/direct.orig" "$UNMATCHED_DIR/direct-no-year.mkv"
    check $? "the file arrives intact"
    [ "$(state_count)" = "0" ] && [ "$(staging_count)" = "0" ]
    check $? "nothing is left behind"
fi

# ---------------------------------------------------------------------------
section "the organise plan"
# ---------------------------------------------------------------------------

# The decision half lives in putio/organize.py and is covered by the Python
# suite against recorded fixtures. What is checked here is that a real run
# applies it to a real filesystem — and, the one that matters, that it never
# deletes from put.io a file that is still sitting in staging.
#
# Nothing here reaches TMDb: every name either fails to parse or is stubbed.
if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the organise checks"
else
    : > "$LOG_FILE"
    PLAN=$(printf 'no-year-here.mkv\n' | putio_python organize-plan 2>/dev/null)
    case "$PLAN" in
        *"PLAN"*"no-year-here.mkv"*"unparsed"*) RC=0 ;;
        *) RC=1 ;;
    esac
    check "$RC" "organize-plan answers for a name that does not parse" \
        "$(printf '%s' "$PLAN" | head -1 | tr -d '\037' | cut -c1-60)"

    # US (0x1f), not a tab. A tab is IFS whitespace, so `IFS=$'\t' read` would
    # collapse runs of them and every field after an empty one would shift
    # left — and empty fields are normal here.
    printf '%s' "$PLAN" | grep -q $'\037'
    check $? "the records are US-delimited"

    # The subcommand answers for whatever it is given — it is a hand tool, and
    # refusing to explain a name would make it useless. The *filtering* is the
    # Planner's, inside the run, and the batch section proves it: a non-video
    # file goes through a whole run without a single [TMDB] line.
    printf 'notes.txt\n' | putio_python organize-plan 2>/dev/null | grep -q 'unparsed'
    check $? "organize-plan explains a name it cannot parse rather than staying silent"

    # A season number with a leading zero. bash's printf read 08 as an octal
    # prefix and filed every S08 and S09 episode into Show/S00 — which is not
    # merely the wrong season, it is Plex's Specials folder.
    S08=$(cd "$SCRIPT_DIR_UNDER_TEST" && "$PYTHON_BIN" -B -c '
from putio.organize import season_directory
print(",".join(season_directory(s) for s in ("1", "08", "09", "10")))' 2>/dev/null)
    [ "$S08" = "S01,S08,S09,S10" ]
    check $? "season 8 goes to S08, not to Plex's Specials folder" "got '$S08'"

    # The file is renamed after the *match*, not left under the release name.
    # A folder saying (2026) around a file saying 2025 is what let Plex match
    # a different film of the same title and attach the download to it, so the
    # two must never be allowed to disagree again.
    NAMES=$(cd "$SCRIPT_DIR_UNDER_TEST" && "$PYTHON_BIN" -B -c '
from putio.organize import canonical_name, episode_code
print(canonical_name("The Good Boy 2025 1080p WEB-DL x265 BONE.mkv", "Good Boy", "2026"))
print(canonical_name("show.s08e02.web.mkv", "Show", "2019", code=episode_code("08", "02")))' 2>/dev/null)
    [ "$NAMES" = "$(printf 'Good Boy (2026).mkv\nShow - S08E02.mkv')" ]
    check $? "a filed name is built from the match, not the release" "got '$NAMES'"

    # ...and it travels to a reader as the ninth field, appended so nothing
    # that already read fields 1-8 has to move.
    PLAN=$(printf 'no-year-here.mkv\n' | putio_python organize-plan 2>/dev/null |
        grep '^PLAN' | head -1 | awk -F'\037' '{print NF}')
    [ "$PLAN" = "9" ]
    check $? "the PLAN record carries the canonical name" "got $PLAN fields"
fi

# ---------------------------------------------------------------------------
section "the archive rules"
# ---------------------------------------------------------------------------

if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the archive checks"
elif ! command -v zip >/dev/null 2>&1; then
    skip "no zip; skipping the archive checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$LOCAL_DIR"/.extracted_* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"

    # archive-check, the hand-run door onto the marker and completeness rules.
    touch "$LOCAL_DIR/Two.rar" "$LOCAL_DIR/Two.r00"
    # CHECK <marker> <extracted> <complete> <parts>
    check_field() { grep '^CHECK ' <<<"$1" | awk "{print \$$2}"; }
    OUT=$(putio_python archive-check Two.rar 2>/dev/null | tr '\037' ' ')
    [ "$(check_field "$OUT" 3)" = "0" ] && [ "$(check_field "$OUT" 4)" = "1" ]
    check $? "a two-volume set is complete and unextracted" "got '$(grep '^CHECK ' <<<"$OUT")'"
    touch "$LOCAL_DIR/.extracted_Two"
    OUT=$(putio_python archive-check Two.rar 2>/dev/null | tr '\037' ' ')
    [ "$(check_field "$OUT" 3)" = "1" ]; check $? "an existing marker is found"
    [ "$(check_field "$OUT" 2)" = ".extracted_Two" ]; check $? "...and named"

    rm -f "$LOCAL_DIR"/* "$LOCAL_DIR"/.extracted_* 2>/dev/null
    touch "$LOCAL_DIR/Gap.r01" "$LOCAL_DIR/Gap.r02"
    OUT=$(putio_python archive-check Gap.r00 2>/dev/null | tr '\037' ' ')
    [ "$(check_field "$OUT" 4)" = "0" ]
    check $? "a missing first volume is incomplete" "got '$(grep '^CHECK ' <<<"$OUT")'"

    # archive-parts decides what gets deleted, so the escaping matters. bash
    # built its part test as an unescaped regex, and a release name is mostly
    # dots — with a base of My.Movie.2019 the unrelated put.io file
    # MyXMovieX2019.r01 matched, and was deleted.
    rm -f "$LOCAL_DIR"/* 2>/dev/null
    touch "$LOCAL_DIR/My.Movie.2019.rar" "$LOCAL_DIR/My.Movie.2019.r00"
    PARTS=$(printf 'My.Movie.2019.rar\nMy.Movie.2019.r00\nMyXMovieX2019.r01\nUnrelated.mkv\n' \
        | putio_python archive-parts My.Movie.2019.rar 2>/dev/null | tr '\037' ' ')
    grep -q "MyXMovieX2019.r01" <<<"$PARTS"
    [ $? -ne 0 ]; check $? "a name that only matches bash's unescaped regex is not a part"
    grep -q "Unrelated.mkv" <<<"$PARTS"
    [ $? -ne 0 ]; check $? "an unrelated file is not a part"
    grep -q "My.Movie.2019.r00" <<<"$PARTS"; check $? "the real volumes are"

    # And the whole thing end to end: an archive arrives, extracts, its
    # contents are filed, and only then is it deleted from put.io.
    rm -f "$LOCAL_DIR"/* "$LOCAL_DIR"/.extracted_* "$REMOTE"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    ZIPSRC="$WORKDIR/zipsrc"
    mkdir -p "$ZIPSRC"
    mkfixture "$ZIPSRC/no-year-inside.mkv" 2
    ( cd "$ZIPSRC" && zip -q "$REMOTE/Arch.zip" "no-year-inside.mkv" )
    ARCH=$(make_processor arch <<'ARCHCFG'
ARCHCFG
)
    run_processor "$ARCH"
    check $? "an archive run succeeds"
    [ -f "$UNMATCHED_DIR/no-year-inside.mkv" ]
    check $? "the extracted file is filed"
    [ ! -e "$REMOTE/Arch.zip" ]
    check $? "an archive whose contents were filed is deleted from put.io"
    ls -a "$LOCAL_DIR" 2>/dev/null | grep -q '^\.extracted_Arch$'
    check $? "an extraction marker is left so the next run does not redo it"
    [ -z "$(ls -A "$STAGING_DIR" 2>/dev/null)" ]
    check $? "the extraction scratch directory does not leak"

    # DELETE_AFTER_DOWNLOAD is the one setting that must be obeyed on the
    # archive path too.
    rm -f "$LOCAL_DIR"/* "$LOCAL_DIR"/.extracted_* "$REMOTE"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    ( cd "$ZIPSRC" && zip -q "$REMOTE/Keep.zip" "no-year-inside.mkv" )
    KEEP=$(make_processor keep <<'KEEPCFG'
DELETE_AFTER_DOWNLOAD="false"
KEEPCFG
)
    run_processor "$KEEP"
    [ -e "$REMOTE/Keep.zip" ]; check $? "DELETE_AFTER_DOWNLOAD=false keeps the put.io copy"
    [ -f "$UNMATCHED_DIR/no-year-inside.mkv" ]; check $? "...and still files the contents"
fi

# ---------------------------------------------------------------------------
section "the download batch, and the invariant it protects"
# ---------------------------------------------------------------------------

# The rule the whole project rests on: delete from put.io only after the local
# file has left staging. That is what makes put.io itself the durable work
# queue, and it is the one thing a bug here could quietly undo.
if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the batch checks"
else
    rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
    : > "$LOG_FILE"
    mkfixture "$REMOTE/no-year-here.mkv" 2
    mkfixture "$REMOTE/soundtrack.flac" 2
    printf 'tiny\n' > "$REMOTE/Sample.mkv"
    BATCH=$(make_processor batch <<'BATCHCFG'
BATCHCFG
)
    run_processor "$BATCH"
    check $? "a mixed listing runs to completion"
    [ -f "$UNMATCHED_DIR/no-year-here.mkv" ]
    check $? "an unparseable video file is filed to Unmatched"
    [ ! -e "$REMOTE/no-year-here.mkv" ]
    check $? "put.io releases a copy whose local file left staging"
    [ ! -e "$REMOTE/Sample.mkv" ]
    check $? "a junk name is deleted from put.io without being downloaded"
    grep -q "\[JUNK\] Skipping junk file: Sample.mkv" "$LOG_FILE"; check $? "...and says so"
    [ ! -e "$REMOTE/soundtrack.flac" ] && [ ! -e "$LOCAL_DIR/soundtrack.flac" ]
    check $? "a non-video file is downloaded, deleted locally and deleted from put.io"
    [ -z "$(ls -A "$STAGING_DIR" 2>/dev/null)" ]; check $? "no staging directory is left behind"
    [ -z "$(ls -A "$PROGRESS_STATE_DIR" 2>/dev/null)" ]; check $? "no progress file outlives its transfer"
    grep -q "=== Script completed successfully — 2 downloaded" "$LOG_FILE"
    check $? "only the files that actually landed are counted"

    # A file the previous run could not file is picked up by the local sweep.
    : > "$LOG_FILE"
    cp "$UNMATCHED_DIR/no-year-here.mkv" "$LOCAL_DIR/no-year-here.mkv"
    rm -f "$UNMATCHED_DIR"/*
    run_processor "$BATCH"
    grep -q "\[LOCAL\] Found 1 unprocessed video file" "$LOG_FILE"
    check $? "the local sweep runs inside the batch, not beside it"
    [ ! -e "$LOCAL_DIR/no-year-here.mkv" ]; check $? "...and files what it finds"
    grep -q "1 local file(s)" "$LOG_FILE"
    check $? "the sweep's count reaches the run summary"

    # The empty-folder pass, which runs after everything else.
    #
    # Nested, because the listing is reversed for a reason: `--recursive` names
    # a parent before its children, so emptying a leaf without going
    # deepest-first would leave its now-empty parent for the *next* run.
    : > "$LOG_FILE"
    rm -rf "$REMOTE"/* "$UNMATCHED_DIR"/* 2>/dev/null
    mkdir -p "$REMOTE/Outer/Inner"
    run_processor "$BATCH"
    [ ! -d "$REMOTE/Outer/Inner" ] && [ ! -d "$REMOTE/Outer" ]
    check $? "nested empty folders are removed parent and child in one pass"

    # And a folder that still holds something is left alone. DELETE_AFTER_DOWNLOAD
    # is off for this one, or the run would drain the folder itself and the
    # check would pass for the wrong reason.
    : > "$LOG_FILE"
    rm -rf "$REMOTE"/* "$UNMATCHED_DIR"/* "$LOCAL_DIR"/* 2>/dev/null
    mkdir -p "$REMOTE/FullDir"
    # Over 1MB, and not a junk extension: anything smaller is judged junk by
    # the size probe and deleted from put.io outright, which would empty the
    # folder and pass this check for entirely the wrong reason.
    mkfixture "$REMOTE/FullDir/keep.flac" 2
    KEEPDIR=$(make_processor keepdir <<'KEEPDIRCFG'
DELETE_AFTER_DOWNLOAD="false"
KEEPDIRCFG
)
    run_processor "$KEEPDIR"
    [ -e "$REMOTE/FullDir/keep.flac" ]
    check $? "DELETE_AFTER_DOWNLOAD=false keeps a non-video file on put.io"
    [ -d "$REMOTE/FullDir" ]; check $? "a folder with something in it is left alone"

    # QUIRK, pinned rather than fixed: the junk delete does NOT honour
    # DELETE_AFTER_DOWNLOAD, so the setting documented as "test run (no
    # deletion)" still destroys every junk file on put.io. Bash did this too.
    # It is marked in putio/pipeline.py; this is the check that would notice if
    # it were ever changed by accident rather than on purpose.
    : > "$LOG_FILE"
    rm -rf "$REMOTE"/* "$LOCAL_DIR"/* 2>/dev/null
    printf 'tiny\n' > "$REMOTE/Sample.mkv"
    run_processor "$KEEPDIR"
    [ ! -e "$REMOTE/Sample.mkv" ]
    check $? "QUIRK: the junk delete ignores DELETE_AFTER_DOWNLOAD"
    rm -rf "$REMOTE"/* "$LOCAL_DIR"/* 2>/dev/null

    # The hand-run door onto the same code. It is documented in CLAUDE.md, so
    # it has to keep working — and it must be the same wiring as the run, or a
    # hand-run batch would behave differently from the real one.
    : > "$LOG_FILE"
    mkfixture "$REMOTE/handrun-no-year.mkv" 2
    # The harness's own sourced settings are the ones putio_python hands over,
    # and two of them have to be what a real run would have: DELETE_AFTER_DOWNLOAD
    # is off in the sandbox by default, and an unset TMDB_API_KEY makes the
    # subcommand refuse before START — a different test entirely.
    SAVED_DELETE="$DELETE_AFTER_DOWNLOAD"; SAVED_TMDB="$TMDB_API_KEY"
    SAVED_REMOTE="$REMOTE"
    DELETE_AFTER_DOWNLOAD="true"
    TMDB_API_KEY="harness-not-a-real-key"
    REMOTE="$REMOTE/"
    OUT=$(printf 'handrun-no-year.mkv\n' | putio_python download-batch 2>/dev/null | tr '\037' ' ')
    REMOTE="$SAVED_REMOTE"
    DELETE_AFTER_DOWNLOAD="$SAVED_DELETE"; TMDB_API_KEY="$SAVED_TMDB"
    grep -q '^START' <<<"$OUT"; check $? "download-batch still reports START before any side effect"
    grep -q '^DONE' <<<"$OUT"; check $? "...and DONE when it reaches the end"
    grep -q '^DOWNLOADED handrun-no-year.mkv' <<<"$OUT"; check $? "...and names what it transferred"
    [ ! -e "$REMOTE/handrun-no-year.mkv" ]; check $? "...and really did the work"
    rm -f "$UNMATCHED_DIR"/* 2>/dev/null
fi

# ---------------------------------------------------------------------------
section "the Plex scan"
# ---------------------------------------------------------------------------

# Everything here runs against a stub HTTP server on 127.0.0.1 — the real Plex
# is on this same machine and the real webhook is on the internet, and a test
# suite must reach neither. PLEX_URL is checked against the stub before
# anything is called.
if [ "$HAVE_PY" != "0" ]; then
    skip "no interpreter; skipping the Plex checks"
else
    PLEX_STUB_LOG="$WORKDIR/plex-stub.requests"
    : > "$PLEX_STUB_LOG"

    cat > "$WORKDIR/plex_stub.py" <<'STUB'
"""A Plex-shaped server on a random loopback port.

Answers /library/sections with the same two-library XML the real server here
returns, 200s a refresh of section 1 and 2, and 404s anything else — which is
what Plex really does, in HTML, and what `curl -s` reported as a success.
"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = sys.argv[1]
SECTIONS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<MediaContainer size="2">\n'
    b'<Directory key="1" type="movie" title="Movies" />\n'
    b'<Directory key="2" type="show" title="TV Shows" />\n'
    b'</MediaContainer>\n'
)
NOT_FOUND = b"<html><head><title>Not Found</title></head><body></body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self):
        # Path only, never the query: it carries X-Plex-Token.
        with open(LOG, "a") as handle:
            handle.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.path.startswith("/library/sections?"):
            self._send(200, SECTIONS)
        else:
            self._send(404, NOT_FOUND)

    def do_POST(self):
        self._record()
        path = self.path.split("?")[0]
        if path in ("/library/sections/1/refresh", "/library/sections/2/refresh"):
            self._send(200, b"")
        elif path == "/hook":
            self._send(200, b"ok")
        else:
            self._send(404, NOT_FOUND)


server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
STUB

    "$PYTHON_BIN" "$WORKDIR/plex_stub.py" "$PLEX_STUB_LOG" > "$WORKDIR/plex-stub.port" 2>/dev/null &
    PLEX_STUB_PID=$!
    # Into the trap immediately, not just at the end of this section: a Ctrl-C
    # mid-run, or the PLEX_URL guard below firing its exit, would otherwise
    # leave a detached server holding a loopback port and writing to a log
    # inside the directory the trap has just deleted.
    trap 'kill "${PLEX_STUB_PID:-}" 2>/dev/null; rm -rf "$WORKDIR"' EXIT
    await '[ -s "$WORKDIR/plex-stub.port" ]' 50
    PLEX_STUB_PORT=$(cat "$WORKDIR/plex-stub.port" 2>/dev/null)

    if [ -z "$PLEX_STUB_PORT" ]; then
        skip "stub server did not start; skipping the Plex checks"
        kill "$PLEX_STUB_PID" 2>/dev/null
    else
        PLEX_URL="http://127.0.0.1:$PLEX_STUB_PORT"
        PLEX_TOKEN="harness-plex-token"
        DROVIE_WEBHOOK_URL="$PLEX_URL/hook"
        DROVIE_WEBHOOK_KEY="harness-webhook-key"

        # The same guard the sandbox has for paths: the live Plex is on this
        # machine and the live webhook is on the internet, so a PLEX_URL that
        # slipped through would trigger a real library scan from a test run.
        case "$PLEX_URL" in
            http://127.0.0.1:*) ;;
            *) printf 'ABORT: PLEX_URL=%s is not the stub\n' "$PLEX_URL" >&2; exit 1 ;;
        esac

        stub_saw() { grep -qF "$1" "$PLEX_STUB_LOG"; }

        # 1. The subcommand, which is still the hand-run door.
        : > "$LOG_FILE"
        : > "$PLEX_STUB_LOG"
        putio_python plex-scan --movie 1 --tv 2 --webhook-delay 0 >"$WORKDIR/scan.out" 2>&1
        check $? "plex-scan reports the scan handled"
        grep -q "^DONE" "$WORKDIR/scan.out"; check $? "...with a DONE record"
        stub_saw "POST /library/sections/1/refresh"; check $? "the movie library was refreshed"
        stub_saw "POST /library/sections/2/refresh"; check $? "the TV library was refreshed"
        stub_saw "POST /hook"; check $? "the Drovie webhook was poked"

        # 2. The headline fix from phase 2. Under the bash, `curl -s` exits 0
        #    on Plex's 404 and the scan was logged as successful.
        : > "$PLEX_STUB_LOG"
        putio_python plex-scan --movie 9999 --webhook-delay 0 >"$WORKDIR/scan.out" 2>&1
        grep -q "ERROR.*Failed to trigger movie library scan" "$WORKDIR/scan.out"
        check $? "a 404 from Plex is reported as a failure"
        grep -q "triggered successfully" "$WORKDIR/scan.out"
        [ $? -ne 0 ]; check $? "...and NOT as a success"
        stub_saw "POST /hook"
        [ $? -ne 0 ]; check $? "no webhook after a failed scan"

        # 3. Discovery, for a config that has not pinned the library ids.
        : > "$PLEX_STUB_LOG"
        putio_python plex-scan --tv "" --webhook-delay 0 >/dev/null 2>&1
        check $? "discovery reports handled"
        stub_saw "GET /library/sections"; check $? "the sections API was queried"
        stub_saw "POST /library/sections/2/refresh"
        check $? "discovery resolves 'tv' to the show library, not the first one"

        # 4. And the scan as a real run does it: only for the libraries that
        #    got something, and after everything else has been organised.
        rm -f "$REMOTE"/* "$LOCAL_DIR"/* "$UNMATCHED_DIR"/* 2>/dev/null
        rm -rf "$MOVIE_LIBRARY"/* "$TV_LIBRARY"/* 2>/dev/null
        : > "$LOG_FILE"
        : > "$PLEX_STUB_LOG"
        mkfixture "$REMOTE/no-year-here.mkv" 2
        SCANRUN=$(make_processor scanrun <<SCANCFG
PLEX_URL="$PLEX_URL"
PLEX_TOKEN="harness-plex-token"
PLEX_MOVIE_LIBRARY_ID="1"
PLEX_TV_LIBRARY_ID="2"
PLEX_REQUEST_WEBHOOK_URL="$PLEX_URL/hook"
PLEX_REQUEST_WEBHOOK_KEY="harness-webhook-key"
SCANCFG
)
        run_processor "$SCANRUN"
        [ ! -s "$PLEX_STUB_LOG" ]
        check $? "a run that filed nothing to a library scans nothing"
        grep -q "Plex scan:" "$LOG_FILE"
        [ $? -ne 0 ]; check $? "...and the summary does not claim one"

        # 5. Secrets. These lines go into a log kept for weeks and read by
        #    putioctl, and the URL is what makes the error messages useful.
        : > "$LOG_FILE"
        putio_python plex-scan --movie 9999 --webhook-delay 0 >>"$LOG_FILE" 2>&1
        grep -q "harness-plex-token" "$LOG_FILE"
        [ $? -ne 0 ]; check $? "the Plex token never reaches the log"
        grep -q "harness-webhook-key" "$LOG_FILE"
        [ $? -ne 0 ]; check $? "the webhook key never reaches the log"

        kill "$PLEX_STUB_PID" 2>/dev/null
        wait "$PLEX_STUB_PID" 2>/dev/null
        trap 'rm -rf "$WORKDIR"' EXIT
    fi
fi

# ---------------------------------------------------------------------------
section "the CLI reads what the processor writes"
# ---------------------------------------------------------------------------

CLI="$SCRIPT_DIR_UNDER_TEST/putioctl"
if [ ! -x "$CLI" ]; then
    skip "no putioctl beside the script; skipping reader checks"
else
    mkdir -p "$PROGRESS_STATE_DIR"
    rm -f "$PROGRESS_STATE_DIR"/dl.* 2>/dev/null
    printf '{"level":"notice","msg":"x","stats":{"bytes":500,"totalBytes":1000,"speed":100,"eta":5}}\n' \
        > "$PROGRESS_STATE_DIR/dl.$$.42.log"
    cat > "$PROGRESS_STATE_DIR/dl.$$.42.meta" <<EOF
RUN_PID=$$
PHASE=download
STARTED=$(date +%s)
SIZE=1000
NAME=Reader.Check.mkv
REMOTE=/Reader.Check.mkv
DEST=$LOCAL_DIR/Reader.Check.mkv
LOG=$PROGRESS_STATE_DIR/dl.$$.42.log
EOF
    OUT=$(PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" "$CLI" --script "$SCRIPT_UNDER_TEST" progress 2>&1)
    grep -q "Reader.Check.mkv" <<<"$OUT"; check $? "putioctl progress names the transfer"
    grep -q "50%" <<<"$OUT"; check $? "putioctl progress computes the percentage"
    grep -qv "No such file" <<<"$OUT"; check $? "putioctl progress prints no shell errors"
    rm -f "$PROGRESS_STATE_DIR"/dl.*
    OUT=$(PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" "$CLI" --script "$SCRIPT_UNDER_TEST" progress 2>&1)
    grep -q "No " <<<"$OUT"; check $? "putioctl progress reports an empty state"

    # And the same, for a meta file Python actually wrote — the protocol is
    # written by putio/progress.py now, and read by three things that are not.
    if [ "$HAVE_PY" = "0" ]; then
        PKG_PARENT="$SCRIPT_DIR_UNDER_TEST" PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" \
        LOCAL_DIR="$LOCAL_DIR" RUN_PID="$$" "$PYTHON_BIN" -B -c '
import os, sys
sys.path.insert(0, os.environ["PKG_PARENT"])
from putio.progress import ProgressPublisher
job = ProgressPublisher(os.environ["PROGRESS_STATE_DIR"], os.environ["RUN_PID"]).begin(
    "Batch.Check.mkv", "1000", os.environ["LOCAL_DIR"] + "/Batch.Check.mkv")
open(job.log_path, "w").write(
    "{\"level\":\"notice\",\"msg\":\"x\",\"stats\":"
    "{\"bytes\":500,\"totalBytes\":1000,\"speed\":100,\"eta\":5}}\n")
' 2>/dev/null
        OUT=$(PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" "$CLI" --script "$SCRIPT_UNDER_TEST" progress 2>&1)
        grep -q "Batch.Check.mkv" <<<"$OUT"; check $? "putioctl reads a meta file Python wrote"
        grep -q "50%" <<<"$OUT"; check $? "...and computes the same percentage from it"
        rm -f "$PROGRESS_STATE_DIR"/dl.*
    fi
fi

# ---------------------------------------------------------------------------
section "the console's transfer journal"
# ---------------------------------------------------------------------------

# The bars the console leaves in its scrolling log. This is a pure diff over
# successive probe snapshots, so it is tested as one — driving the real curses
# console would need a pty and would prove less. What matters is the emit
# policy: too eager and ~900 lines per download push the processor's own lines
# out of a buffer that holds MAX_BUFFER_LINES; too lazy and a finished
# transfer leaves nothing behind, which is the state this replaced.
TUI_PY="$SCRIPT_DIR_UNDER_TEST/putioctl_tui.py"
if [ ! -f "$TUI_PY" ]; then
    skip "no putioctl_tui.py beside the script; skipping journal checks"
elif [ "$HAVE_PY" != "0" ]; then
    skip "no python3; skipping journal checks"
else
    TRACE=$(TUI_PY="$TUI_PY" "$PYTHON_BIN" -B - <<'PYEOF' 2>&1
import importlib.util, os

spec = importlib.util.spec_from_file_location("tui", os.environ["TUI_PY"])
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)

SIZE = 1000

def snap(pct, phase="download"):
    done = None if pct is None else SIZE * pct // 100
    return [{"key": "dl.1.7", "name": "Journal.Check.mkv", "size": SIZE,
             "bytes": done, "phase": phase, "speed": 100, "eta": 5}]

journal = tui.TransferJournal()
steps = [snap(None), snap(5), snap(9), snap(12), snap(19), snap(20),
         snap(0, "copy")]
same = snap(50, "copy")
steps += [same, same, []]

for rows in steps:
    for row, note in journal.update(rows):
        pct = tui.transfer_pct(row)
        print("EMIT {0} {1} {2}".format(
            "none" if pct is None else pct, row["phase"], note or "-"))
    print("TICK")

# A retry: `Retrier` re-runs rclone with the same --log-file and a fresh
# process counts from zero, so the bar goes backwards. That is a restart, not
# jitter, and it must leave a line — otherwise the whole second attempt of a
# multi-gigabyte download is silent, which is the case the journal most exists
# to explain.
retry = tui.TransferJournal()
for pct in (30, 60, 85):
    retry.update(snap(pct))
seen = []
for pct in (2, 12, 22):
    for row, _note in retry.update(snap(pct)):
        seen.append(str(tui.transfer_pct(row)))
print("RETRY " + " ".join(seen))

# Losing sight of the percentage is *not* a restart: a dropped share makes the
# copy phase's getsize() fail and `bytes` go None, and a line every couple of
# seconds for as long as the share stays away would be noise.
blind = tui.TransferJournal()
blind.update(snap(50, "copy"))
print("BLIND {0}{1}".format(len(blind.update(snap(None, "copy"))),
                            len(blind.update(snap(None, "copy")))))

# The rendered line: a note rides every rung of the width ladder, and an
# ordinary strip row (no note) must be untouched by any of this.
row = snap(43)[0]
print("WIDE " + "".join(t for t, _ in tui.transfer_segments(row, 100, "ended")).rstrip())
print("NARROW " + "".join(t for t, _ in tui.transfer_segments(row, 46, "ended")).rstrip())
print("LIVE " + "".join(t for t, _ in tui.transfer_segments(row, 100)).rstrip())
PYEOF
)

    EXPECTED='EMIT none download -
TICK
EMIT 5 download -
TICK
TICK
EMIT 12 download -
TICK
TICK
EMIT 20 download -
TICK
EMIT 0 copy -
TICK
EMIT 50 copy -
TICK
TICK
EMIT 50 copy ended
TICK'
    [ "$(grep -E '^(EMIT|TICK)' <<<"$TRACE")" = "$EXPECTED" ]
    check $? "the journal emits on first sight, each 10%, each phase change and once at the end" \
        "got: $(tr '\n' '|' <<<"$TRACE")"

    grep -q '^EMIT 9 ' <<<"$TRACE"; [ $? = 1 ]
    check $? "...and nothing at all while the bar stays inside one bucket"
    [ "$(grep -c 'ended' <<<"$TRACE" )" -ge 1 ] && [ "$(grep -c '^EMIT.*ended' <<<"$TRACE")" = 1 ]
    check $? "a transfer that leaves the strip closes with exactly one line"

    grep -q '^RETRY 2 12 22$' <<<"$TRACE"
    check $? "a bar that restarts from zero is a retry, and does not go unrecorded"
    grep -q '^BLIND 00$' <<<"$TRACE"
    check $? "...but a percentage that goes unreadable is not a restart"

    grep -q '^WIDE .*█.* 43%.*· ended' <<<"$TRACE"
    check $? "the closing line is the bar as it last stood, marked"
    grep -q '^NARROW .*ended' <<<"$TRACE"
    check $? "...and the mark survives a terminal too narrow for the numbers"
    grep -q '^LIVE .*43%' <<<"$TRACE" && ! grep -q '^LIVE .*ended' <<<"$TRACE"
    check $? "an ordinary strip row is unchanged"
fi

# ---------------------------------------------------------------------------
section "the JSON contract"
# ---------------------------------------------------------------------------

# `--json` is what putio-statusd reads, and through it Drovie and
# the Drovie app. A TestFlight build keeps polling long after the server has moved
# on, so the shape is a contract: fields may be added, but `schema` is bumped
# whenever one changes meaning.
if [ ! -x "$CLI" ]; then
    skip "no putioctl beside the script; skipping JSON checks"
elif ! command -v jq >/dev/null 2>&1; then
    skip "no jq; skipping JSON checks"
else
    mkdir -p "$PROGRESS_STATE_DIR"
    rm -f "$PROGRESS_STATE_DIR"/dl.*

    JSON=$(PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" "$CLI" --script "$SCRIPT_UNDER_TEST" status --json 2>&1)
    jq -e . >/dev/null 2>&1 <<<"$JSON"; check $? "status --json emits valid JSON"
    [ "$(jq -r '.schema' <<<"$JSON" 2>/dev/null)" = "1" ]; check $? "status --json carries schema 1"
    jq -e 'has("state") and has("transfers") and has("last_run") and has("mount") and has("disk")' \
        >/dev/null 2>&1 <<<"$JSON"; check $? "status --json carries every top-level key callers read"
    [ "$(jq -r '.transfers | type' <<<"$JSON" 2>/dev/null)" = "array" ]
    check $? "transfers is an array when nothing is running"

    # Release names arrive from put.io carrying quotes, backslashes and
    # non-ASCII. This is the case hand-rolled escaping gets wrong, which is why
    # the JSON is built with jq rather than printf.
    NASTY='Sh0w "Quoted" & back\slash — ünïcode S01E01 [1080p].mkv'
    printf '{"level":"notice","msg":"x","stats":{"bytes":500,"totalBytes":1000,"speed":100,"eta":5}}\n' \
        > "$PROGRESS_STATE_DIR/dl.$$.77.log"
    {
        echo "RUN_PID=$$"
        echo "PHASE=download"
        echo "STARTED=$(date +%s)"
        echo "SIZE=1000"
        echo "NAME=$NASTY"
        echo "REMOTE=/$NASTY"
        echo "DEST=$LOCAL_DIR/$NASTY"
        echo "LOG=$PROGRESS_STATE_DIR/dl.$$.77.log"
    } > "$PROGRESS_STATE_DIR/dl.$$.77.meta"

    JSON=$(PROGRESS_STATE_DIR="$PROGRESS_STATE_DIR" "$CLI" --script "$SCRIPT_UNDER_TEST" progress --json 2>&1)
    jq -e . >/dev/null 2>&1 <<<"$JSON"
    check $? "progress --json survives a name with quotes, backslash and unicode"
    [ "$(jq -r '.[0].name' <<<"$JSON" 2>/dev/null)" = "$NASTY" ]; check $? "the name round-trips byte for byte"
    [ "$(jq -r '.[0].percent' <<<"$JSON" 2>/dev/null)" = "50" ]; check $? "progress --json computes the percentage"
    [ "$(jq -r '.[0].bytes_done' <<<"$JSON" 2>/dev/null)" = "500" ]
    check $? "progress --json reports rclone's byte count"
    rm -f "$PROGRESS_STATE_DIR"/dl.*
fi

# ---------------------------------------------------------------------------
section "the status endpoint"
# ---------------------------------------------------------------------------

# The daemon must answer while putioctl is wedged — a stat on a dropped SMB
# share sits in the kernel, and a handler that waited on it would hang
# Drovie and the Drovie app behind it. So: a refresher thread owns the
# snapshot, handlers only ever serialise the last good one.
STATUSD="$SCRIPT_DIR_UNDER_TEST/putio-statusd"
if [ ! -x "$STATUSD" ]; then
    skip "no putio-statusd beside the script; skipping endpoint checks"
elif ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    skip "need curl and python3; skipping endpoint checks"
else
    PORT=19531
    # The stand-in hangs via its own copy of sleep rather than /bin/sleep, so
    # the pgrep and pkill below match this run and nothing else. Matching
    # `sleep 600` by command line would reach every process on the machine.
    # $WORKDIR is a fresh mktemp path, so the name is unique per run.
    HANGSLEEP="$WORKDIR/statusd-test-hang"
    printf '#!/bin/sh\nexec sleep "$1"\n' > "$HANGSLEEP"
    chmod +x "$HANGSLEEP"

    FAKE="$WORKDIR/fake-putioctl"
    cat > "$FAKE" <<FAKEEOF
#!/bin/bash
[ -f "$WORKDIR/wedge" ] && { "$HANGSLEEP" 600 & "$HANGSLEEP" 600; exit 0; }
echo '{"schema":1,"state":"running","transfers":[{"name":"Endpoint.Check.mkv","percent":68}],"disk":{"free_bytes":1}}'
FAKEEOF
    chmod +x "$FAKE"

    python3 "$STATUSD" --putioctl "$FAKE" --port "$PORT" --interval 1 --timeout 2 \
        > "$WORKDIR/statusd.log" 2>&1 &
    SD_PID=$!
    # Wait for a real reading, not merely for the socket: a numeric success
    # count is the only thing that says the refresher has completed a cycle.
    SD_UP=1
    for _ in $(seq 1 60); do
        n=$(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null | jq -r '.successes // 0' 2>/dev/null)
        # Not "${n:-0}": that defaults the *test*, leaving n itself empty for
        # the comparison below, which then errors instead of looping.
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        [ "$n" -ge 1 ] && { SD_UP=0; break; }
        sleep 0.25
    done
    check "$SD_UP" "the daemon comes up and takes a reading"

    if [ "$SD_UP" -eq 0 ]; then
        BODY=$(curl -s -m 5 "http://127.0.0.1:$PORT/status")
        [ "$(jq -r '.transfers[0].name' <<<"$BODY" 2>/dev/null)" = "Endpoint.Check.mkv" ]
        check $? "/status serves what putioctl reported"
        jq -e 'has("stale_seconds")' >/dev/null 2>&1 <<<"$BODY"
        check $? "/status stamps how old the reading is"
        [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/transfers")" = "200" ]
        check $? "/transfers answers"
        [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/nope")" = "404" ]
        check $? "an unknown path is a 404, not a stack trace"

        # The whole point: wedge the source and the endpoint must stay fast.
        touch "$WORKDIR/wedge"
        sleep 6
        START=$(date +%s)
        BODY=$(curl -s -m 5 "http://127.0.0.1:$PORT/status")
        ELAPSED=$(( $(date +%s) - START ))
        [ "$ELAPSED" -le 1 ]
        check $? "/status still answers immediately while putioctl is wedged" "took ${ELAPSED}s"
        [ "$(jq -r '.transfers[0].name' <<<"$BODY" 2>/dev/null)" = "Endpoint.Check.mkv" ]
        check $? "the last good reading is served rather than blanked"
        [ "$(jq -r '.stale_seconds > 3' <<<"$BODY" 2>/dev/null)" = "true" ]
        check $? "stale_seconds shows the reading has aged"
        jq -e 'has("source_error")' >/dev/null 2>&1 <<<"$BODY"
        check $? "source_error says why the reading stopped refreshing"
        [ "$(pgrep -f "$HANGSLEEP" | wc -l | tr -d ' ')" -le 2 ]
        check $? "wedged children are killed by process group, not left to pile up"

        rm -f "$WORKDIR/wedge"
        sleep 3
        BODY=$(curl -s -m 5 "http://127.0.0.1:$PORT/status")
        [ "$(jq -r '.stale_seconds < 3' <<<"$BODY" 2>/dev/null)" = "true" ]
        check $? "it recovers on its own once the source responds again"
    fi
    kill "$SD_PID" 2>/dev/null
    wait "$SD_PID" 2>/dev/null
    pkill -f "$HANGSLEEP" 2>/dev/null
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]
