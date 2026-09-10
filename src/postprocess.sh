#!/usr/bin/env bash
#
# postprocess.sh -- chopcam node housekeeping.
#
# Runs on a 5-minute timer at idle priority. Three independent stages, so a
# failure in one never blocks the others:
#
#   1. TRANSCODE  raw MJPEG (.mkv)  ->  H.264 (.mp4)   -- ON THIS PI
#   2. SHIP       H.264             ->  aggregator      -- verified
#   3. PURGE      delivered clips past retention + disk-pressure guard
#
# The H.264 encode happens HERE, not in capture.py, on purpose. capture.py
# writes MJPEG in ~0.1 s so it never stalls the 120 fps buffer; libx264 (~6 fps
# on this material, so ~10 min per 30 s clip on a Pi 4) runs later at idle
# priority where it cannot cost you frames. Same output, same machine.
#
# Nothing is deleted until the next stage confirms it has the data.

set -uo pipefail

# ---------------------------------------------------------------------------
# Config -- shared with capture.py. See chopcam.conf.example.
# ---------------------------------------------------------------------------
CONF="${CHOPCAM_CONF:-/etc/chopcam.conf}"
if [[ ! -f "$CONF" ]]; then
    alt="$(dirname "$(readlink -f "$0")")/../chopcam.conf"
    [[ -f "$alt" ]] && CONF="$alt"
fi
if [[ ! -f "$CONF" ]]; then
    echo "No config found at $CONF -- copy chopcam.conf.example to /etc/chopcam.conf" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

NODE_NAME="${NODE_NAME:-chop1}"
STATE_DIR="${STATE_DIR:-/var/lib/chopcam}"
RAW_DIR="$STATE_DIR/raw"           # capture.py writes MJPEG .mkv here
ENC_DIR="$STATE_DIR/encoded"       # H.264 .mp4 awaiting transfer
SENT_DIR="$STATE_DIR/sent"         # delivered; kept briefly as a safety copy

SHIP_ENABLED="${SHIP_ENABLED:-false}"
AGG_HOST="${AGG_USER:-user}@${AGG_IP:-127.0.0.1}"
AGG_DIR="${AGG_DIR:-/tmp}"
VERIFY_MODE="${VERIFY_MODE:-hash}"
AGG_OS="${AGG_OS:-linux}"
AGG_PORT="${AGG_PORT:-22}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-3}"
DISK_PCT_LIMIT="${DISK_PCT_LIMIT:-85}"
CRF="${CRF:-23}"
PRESET="${PRESET:-veryfast}"
PLAYBACK_MODE="${PLAYBACK_MODE:-realtime}"

# ---------------------------------------------------------------------------

log() { echo "[$(date '+%H:%M:%S')] $*"; }

mkdir -p "$RAW_DIR" "$ENC_DIR" "$SENT_DIR"

# One instance at a time; -n fails fast rather than queueing timer runs.
exec 9>"$STATE_DIR/.postprocess.lock"
if ! flock -n 9; then
    log "another postprocess run is active; exiting"
    exit 0
fi

# ssh takes -p for the port, scp takes -P, so they need separate arrays.
_SSH_COMMON=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
SSH_OPTS=("${_SSH_COMMON[@]}" -p "$AGG_PORT")
SCP_OPTS=("${_SSH_COMMON[@]}" -P "$AGG_PORT")

# Count video packets: more reliable than nb_frames, which MJPEG often omits.
frame_count() {
    ffprobe -v error -select_streams v:0 -count_packets \
            -show_entries stream=nb_read_packets \
            -of csv=p=0 "$1" 2>/dev/null | tr -d '\r\n'
}

# ---------------------------------------------------------------------------
# STAGE 0 -- clear crash debris
# ---------------------------------------------------------------------------
# capture.py writes each clip to ".<name>.part.mkv" and renames it into place,
# and stage 1 below does the same with ".<name>.part.mp4". A crash or a kill
# mid-write leaves the dot-prefixed file behind. Nothing ever legitimately
# takes an hour to write (a clip mux is ~0.1 s), so anything older than that is
# debris. This is crash hygiene, not retention -- no completed clip is touched.
stale="$(find "$RAW_DIR" "$ENC_DIR" -maxdepth 1 -name '.*.part.*' -type f \
         -mmin +60 -print -delete 2>/dev/null | wc -l)"
[[ "$stale" -gt 0 ]] && log "cleared $stale abandoned .part file(s)"

# ---------------------------------------------------------------------------
# STAGE 1 -- transcode MJPEG -> H.264 (on this Pi)
# ---------------------------------------------------------------------------
shopt -s nullglob

# NOTE: this glob does not match dotfiles, which is exactly what keeps a clip
# capture.py is still writing (".<name>.part.mkv") out of the loop.
for raw in "$RAW_DIR"/*.mkv; do
    base="$(basename "$raw" .mkv)"
    out="$ENC_DIR/${base}.mp4"
    tmp="$ENC_DIR/.${base}.part.mp4"

    [[ -e "$out" ]] && { log "already encoded, skipping: $base"; continue; }

    # Belt and braces behind the .part naming above: skip anything touched in
    # the last 30 s, in case a clip arrived here by some other route.
    if [[ -n "$(find "$raw" -mmin -0.5 2>/dev/null)" ]]; then
        log "still being written, will retry: $base"
        continue
    fi

    src_frames="$(frame_count "$raw")"
    log "transcoding $base (${src_frames:-?} frames)"

    if [[ "$PLAYBACK_MODE" == "slowmo" ]]; then
        # Keep every frame but stamp them at 30 fps, so the clip plays back at
        # 1/4 speed in any player without the viewer doing anything.
        timing=(-vf "setpts=4.0*PTS" -r 30)
    else
        timing=()
    fi

    rm -f "$tmp"
    if ! ffmpeg -nostdin -hide_banner -loglevel error -y \
            -i "$raw" "${timing[@]}" \
            -c:v libx264 -preset "$PRESET" -crf "$CRF" \
            -pix_fmt yuv420p -movflags +faststart \
            "$tmp"; then
        log "ERROR: ffmpeg failed on $base -- keeping raw for retry"
        rm -f "$tmp"
        continue
    fi

    # Guard against a silent 120->30 fps drop, which would quietly destroy the
    # slow-motion detail this whole system exists to capture.
    out_frames="$(frame_count "$tmp")"
    if [[ -n "$src_frames" && -n "$out_frames" && "$out_frames" -lt "$src_frames" ]]; then
        log "ERROR: frame loss on $base ($src_frames -> $out_frames); keeping raw"
        rm -f "$tmp"
        continue
    fi

    mv "$tmp" "$out"
    rm -f "$raw"            # ~350 MB raw dropped only after a verified encode
    log "encoded $base ($(du -h "$out" | cut -f1), ${out_frames:-?} frames)"
done

# ---------------------------------------------------------------------------
# STAGE 2 -- ship to aggregator, verify, retain locally
# ---------------------------------------------------------------------------

# Remote helpers. AGG_OS selects the command set: the aggregator is a Pi 5
# running Pi OS, so "linux" is the default. "windows" is kept for a PowerShell
# aggregator, where the quoting through ssh -> cmd -> powershell is finicky.
#
# Getting this wrong is not a loud failure: scp still succeeds, verification
# comes back empty, the clip is never marked delivered, and the SAME clip is
# re-shipped every timer run forever -- filling the aggregator with duplicates
# while the node's encoded/ never drains.

# Single-quote a string for safe interpolation into a remote shell command.
shq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

remote_hash() {
    case "$AGG_OS" in
        windows)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "powershell -NoProfile -Command \"(Get-FileHash -Algorithm SHA256 -LiteralPath '$1').Hash\"" \
                2>/dev/null | tr -d '\r\n' | tr '[:upper:]' '[:lower:]'
            ;;
        *)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "sha256sum -- $(shq "$1")" \
                2>/dev/null | cut -d' ' -f1 | tr -d '\r\n' | tr '[:upper:]' '[:lower:]'
            ;;
    esac
}

remote_size() {
    case "$AGG_OS" in
        windows)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "powershell -NoProfile -Command \"(Get-Item -LiteralPath '$1').Length\"" \
                2>/dev/null | tr -dc '0-9'
            ;;
        *)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "stat -c%s -- $(shq "$1")" \
                2>/dev/null | tr -dc '0-9'
            ;;
    esac
}

remote_mkdir() {
    case "$AGG_OS" in
        windows)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '$AGG_DIR' | Out-Null\"" \
                >/dev/null 2>&1
            ;;
        *)
            ssh "${SSH_OPTS[@]}" "$AGG_HOST" \
                "mkdir -p -- $(shq "$AGG_DIR")" \
                >/dev/null 2>&1
            ;;
    esac
}

if [[ "$SHIP_ENABLED" != "true" ]]; then
    pending="$(ls -1 "$ENC_DIR"/*.mp4 2>/dev/null | wc -l)"
    log "auto-transfer off; $pending H.264 clip(s) waiting in $ENC_DIR"

elif remote_mkdir; then

    for clip in "$ENC_DIR"/*.mp4; do
        base="$(basename "$clip")"
        remote="$AGG_DIR/$base"

        log "shipping $base"
        if ! scp -q "${SCP_OPTS[@]}" "$clip" "$AGG_HOST:$remote" 2>/dev/null; then
            log "transfer failed for $base -- will retry next run"
            continue
        fi

        # Confirm receipt by content, not by exit status: scp can succeed while
        # the far end truncated the file (out of disk), and since we delete the
        # local copy afterwards a false success would lose footage for good.
        ok=0
        blank=0          # remote command returned nothing at all
        case "$VERIFY_MODE" in
            hash)
                lsum="$(sha256sum "$clip" | cut -d' ' -f1)"
                rsum="$(remote_hash "$remote")"
                if [[ -z "$rsum" ]]; then
                    blank=1; log "hash check got no answer for $base"
                elif [[ "$lsum" == "$rsum" ]]; then ok=1
                else log "hash MISMATCH for $base -- the copy on the aggregator" \
                        "differs from the local file (truncated or corrupt)"; fi
                ;;
            size)
                lsz="$(stat -c%s "$clip")"
                rsz="$(remote_size "$remote")"
                if [[ -z "$rsz" ]]; then
                    blank=1; log "size check got no answer for $base"
                elif [[ "$lsz" == "$rsz" ]]; then ok=1
                else log "size MISMATCH for $base (local=$lsz remote=$rsz)" \
                        "-- the copy on the aggregator is incomplete"; fi
                ;;
            none) ok=1 ;;
        esac

        if [[ "$ok" -eq 1 ]]; then
            mv "$clip" "$SENT_DIR/$base"
            log "delivered $base (verified: $VERIFY_MODE)"
        else
            log "KEEPING local copy of $base -- will retry next run"
            if [[ "$blank" -eq 1 ]]; then
                log "  The verify command returned nothing, which usually means"
                log "  AGG_OS=\"$AGG_OS\" is wrong for this aggregator (a Pi 5"
                log "  running Pi OS is \"linux\"). Until it matches, the copy"
                log "  lands but cannot be read back, so this clip re-ships"
                log "  every run. VERIFY_MODE=\"size\" is the fallback."
            fi
        fi
    done
else
    log "aggregator unreachable ($AGG_HOST:$AGG_PORT) -- clips held locally"
    log "  Either the host is down, key auth is not set up, or AGG_OS=\"$AGG_OS\""
    log "  is wrong: preparing the destination folder uses OS-specific commands,"
    log "  so a Pi 5 aggregator configured as \"windows\" fails here too."
    log "  Check with:  ssh -p $AGG_PORT $AGG_HOST true"
fi

# ---------------------------------------------------------------------------
# STAGE 3 -- purge
# ---------------------------------------------------------------------------

deleted="$(find "$SENT_DIR" -name '*.mp4' -type f \
           -mtime "+$LOCAL_RETENTION_DAYS" -print -delete | wc -l)"
[[ "$deleted" -gt 0 ]] && log "purged $deleted clip(s) past ${LOCAL_RETENTION_DAYS}d retention"

# Emergency: delete oldest DELIVERED clips only. Never touches raw/ or
# encoded/, so nothing unshipped is ever lost to a purge.
disk_pct="$(df --output=pcent "$STATE_DIR" | tail -1 | tr -dc '0-9')"
while [[ "${disk_pct:-0}" -ge "$DISK_PCT_LIMIT" ]]; do
    oldest="$(find "$SENT_DIR" -name '*.mp4' -type f -printf '%T@ %p\n' \
              2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)"
    [[ -z "$oldest" ]] && { log "WARNING: disk ${disk_pct}% full, nothing left to purge"; break; }
    rm -f "$oldest"
    log "disk ${disk_pct}% -- emergency purge $(basename "$oldest")"
    disk_pct="$(df --output=pcent "$STATE_DIR" | tail -1 | tr -dc '0-9')"
done

exit 0
