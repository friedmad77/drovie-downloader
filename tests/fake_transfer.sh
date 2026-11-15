#!/bin/bash
#
# Fake an in-flight download so the progress readers have something to draw.
# Publishes the same two files the processor does, keyed on this script's own
# pid so `putioctl progress` and the console treat it as a live transfer.
# Touches nothing real: no put.io, no rclone, no media share.
#
# Usage: tests/fake_transfer.sh [name] [size-bytes] [seconds]
# Ctrl-C at any point; the files are removed on the way out.

DIR="${PROGRESS_STATE_DIR:-/tmp/putio_progress}"
NAME="${1:-The.Fake.Movie.2026.2160p.HDR-DEMO.mkv}"
SIZE="${2:-16000000000}"
SECS="${3:-90}"
COPY_SECS=$(( SECS / 3 ))            # last third demonstrates the copy phase
FAKE_DEST="/tmp/putio_fake_dest.bin"

mkdir -p "$DIR" || exit 1
META="$DIR/dl.$$.1.meta"
LOG="$DIR/dl.$$.1.log"
STARTED=$(date +%s)

cleanup() { rm -f "$META" "$LOG" "$FAKE_DEST"; printf '\ncleaned up\n'; exit 0; }
trap cleanup INT TERM EXIT

write_meta() {   # phase dest
    cat > "$META.tmp" <<EOF
RUN_PID=$$
PHASE=$1
STARTED=$STARTED
SIZE=$SIZE
NAME=$NAME
REMOTE=/$NAME
DEST=$2
LOG=$LOG
EOF
    mv -f "$META.tmp" "$META"
}

printf 'Faking "%s" for %ss.\n' "$NAME" "$SECS"
printf 'Watch it with:  putioctl progress -w        (or bare `putioctl` for the console)\n\n'

# --- download phase: append rclone-shaped stats lines ----------------------
write_meta download "$FAKE_DEST"
i=0
while [ "$i" -lt "$(( SECS - COPY_SECS ))" ]; do
    bytes=$(( SIZE * i / (SECS - COPY_SECS) ))
    printf '{"level":"notice","msg":"fake","stats":{"bytes":%s,"totalBytes":%s,"speed":%s,"eta":%s}}\n' \
        "$bytes" "$SIZE" "$(( SIZE / SECS ))" "$(( SECS - COPY_SECS - i ))" >> "$LOG"
    printf '\r  download %3s%%' "$(( i * 100 / (SECS - COPY_SECS) ))"
    sleep 1
    i=$(( i + 1 ))
done

# --- copy phase: readers measure the destination file, so grow one ---------
# Sparse (dd seek), so a 16 GB "file" costs no disk at all.
write_meta copy "$FAKE_DEST"
i=0
while [ "$i" -le "$COPY_SECS" ]; do
    dd if=/dev/null of="$FAKE_DEST" bs=1 seek=$(( SIZE * i / COPY_SECS )) 2>/dev/null
    printf '\r  copy     %3s%%' "$(( i * 100 / COPY_SECS ))"
    sleep 1
    i=$(( i + 1 ))
done
printf '\n  done\n'
