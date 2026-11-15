#!/bin/sh
# `docker exec` runs as root, but the Downloader runs as PUID:PGID. Drop to
# that user before running the CLI, so a `sync` started from here never leaves
# root-owned files in the library, the logs or the config volume.
# Installed as /usr/local/bin/drovie-downloader and /usr/local/bin/putioctl;
# the name it was called by picks the matching link in /app, so help text
# names the command you typed.
name=$(basename "$0")
case "$name" in putioctl|drovie-downloader) ;; *) name=drovie-downloader ;; esac
target="/app/$name"
if [ "$(id -u)" = "0" ] && [ "${PUID:-1000}" != "0" ]; then
    exec setpriv --reuid="${PUID:-1000}" --regid="${PGID:-1000}" --clear-groups -- "$target" "$@"
fi
exec "$target" "$@"
