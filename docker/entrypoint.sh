#!/bin/bash
#
# Container entrypoint: first-start setup, then the status service and the
# scheduler loop that stands in for cron.
#
# tini is PID 1 and forwards signals here. On SIGTERM the run in progress is
# sent SIGTERM too — its handler releases the lock and clears its progress
# files — and the container exits once it has.

set -uo pipefail

APP_DIR="${APP_DIR:-/app}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
CONFIG_FILE="$CONFIG_DIR/.putio_config"
SCRIPT="$APP_DIR/putio_download_and_delete.sh"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

say() { printf '[drovie-downloader] %s\n' "$*"; }

# Run as PUID:PGID so files land in the library owned by you, not root.
as_user() {
    if [ "$PUID" = "0" ] || [ "$(id -u)" != "0" ]; then
        "$@"
    else
        setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
    fi
}

# --- first start ------------------------------------------------------------
mkdir -p "$CONFIG_DIR/logs"
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$APP_DIR/docker/drovie-downloader.conf" "$CONFIG_FILE"
    say "Wrote $CONFIG_FILE (settings fall back to the environment)"
fi
if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    # Only the config volume: the media folders are yours to own, and a
    # recursive chown of a library would take hours and change what Plex sees.
    chown -R "$PUID:$PGID" "$CONFIG_DIR" 2>/dev/null || say "Could not chown $CONFIG_DIR to $PUID:$PGID"
fi

# --- settings the entrypoint itself needs -----------------------------------
# Sourced in a subshell so the processor still reads the file itself, fresh,
# on every run (edits apply at the next sync without a restart).
read_setting() { ( set +u; . "$CONFIG_FILE" >/dev/null 2>&1; eval "printf '%s' \"\${$1:-}\"" ); }
REMOTE_VALUE="$(read_setting REMOTE)"
REMOTE_NAME="${REMOTE_VALUE%%:*}"

# --- put.io remote from PUTIO_TOKEN ------------------------------------------
# Written into $RCLONE_CONFIG on the /config volume (not just exported), so a
# `docker compose exec downloader drovie-downloader sync` sees the same remote
# as the loop does. setup-putio checks put.io accepts the token and creates the
# folder; a failure here is logged, not fatal — the token may be fixed later.
if [ -n "${PUTIO_TOKEN:-}" ]; then
    # The token travels in the environment, not argv, which `ps` would show.
    as_user "$APP_DIR/putioctl" setup-putio 2>&1 | sed 's/^/[drovie-downloader] /'
    true
    [ -f "${RCLONE_CONFIG:-}" ] && chmod 600 "$RCLONE_CONFIG" 2>/dev/null
fi
unset PUTIO_TOKEN

# --- preflight: say what's missing before the first run does ------------------
missing=""
[ -n "$(read_setting TMDB_API_KEY)" ] || missing="$missing TMDB_API_KEY"
[ -n "$(read_setting PLEX_TOKEN)" ] || missing="$missing PLEX_TOKEN"
# Only for an rclone remote ("name:folder"); a plain path needs no token.
if [ "$REMOTE_NAME" != "$REMOTE_VALUE" ] &&
   ! { [ -f "${RCLONE_CONFIG:-}" ] && grep -q "^\[$REMOTE_NAME\]" "$RCLONE_CONFIG"; }; then
    missing="$missing PUTIO_TOKEN"
fi
if [ -n "$missing" ]; then
    say "Not configured yet — set:${missing}. Syncs will refuse to start until then."
fi
for dir in "$(read_setting LOCAL_DIR)" "$(read_setting MOVIE_LIBRARY)" "$(read_setting TV_LIBRARY)"; do
    [ -n "$dir" ] || continue
    if [ ! -d "$dir" ]; then
        say "WARNING: $dir does not exist — mount it (see the compose file)"
    elif ! as_user test -w "$dir"; then
        say "WARNING: $dir is not writable by $PUID:$PGID — fix its owner, or set PUID/PGID"
    fi
done

# --- schedule ----------------------------------------------------------------
N="${SYNC_INTERVAL_MINUTES:-5}"
case "$N" in ''|*[!0-9]*|0) say "SYNC_INTERVAL_MINUTES='$N' is not a whole number of minutes; using 5"; N=5 ;; esac
export SYNC_SCHEDULE="*/$N * * * *"

# Seconds until the next `*/N` minute, by the same arithmetic putioctl uses to
# predict it, so the "next run" Drovie shows is the one that happens.
until_next_slot() {
    local min sec cand
    min=$((10#$(date +%M))); sec=$((10#$(date +%S)))
    cand=$(( (min / N + 1) * N ))
    [ "$cand" -ge 60 ] && cand=60
    printf '%s' $(( (cand - min) * 60 - sec ))
}

# --- one-shot commands --------------------------------------------------------
# `docker compose run --rm downloader status` (or setup-putio, config list, …)
# runs one CLI command with the same settings and exits.
if [ $# -gt 0 ]; then
    as_user "$APP_DIR/drovie-downloader" "$@"
    exit $?
fi

# --- run ---------------------------------------------------------------------
STATUSD_PID=""
RUN_PID=""
SLEEP_PID=""
shutdown() {
    say "Stopping"
    [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null
    [ -n "$RUN_PID" ] && kill -TERM "$RUN_PID" 2>/dev/null && wait "$RUN_PID" 2>/dev/null
    [ -n "$STATUSD_PID" ] && kill -TERM "$STATUSD_PID" 2>/dev/null && wait "$STATUSD_PID" 2>/dev/null
    exit 0
}
trap shutdown TERM INT

# Status service for Drovie (http://<this container>:9494/status).
as_user python3 "$APP_DIR/putio-statusd" \
    --putioctl "$APP_DIR/putioctl" --script "$SCRIPT" &
STATUSD_PID=$!
say "Status service on ${STATUSD_BIND:-127.0.0.1}:${STATUSD_PORT:-9494}; syncing every $N minute(s)"

while true; do
    as_user "$SCRIPT" </dev/null &
    RUN_PID=$!
    wait "$RUN_PID"
    RUN_PID=""
    sleep "$(until_next_slot)" &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done
