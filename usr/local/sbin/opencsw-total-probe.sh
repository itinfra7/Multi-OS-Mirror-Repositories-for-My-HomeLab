#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

BASE=/storage/opencsw
MONITOR_DIR="$BASE/monitor"
LOG_DIR="$BASE/logs"
OUT="$MONITOR_DIR/remote-total.json"
TMP="$OUT.tmp"
LOG="$LOG_DIR/remote-total-probe.log"
SRC="rsync://mirror.opencsw.org/opencsw-full/"
LOCK=/run/lock/opencsw-total-probe.lock

mkdir -p "$MONITOR_DIR" "$LOG_DIR" /run/lock

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

{
  echo "probe-start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  rsync -aH --dry-run --stats "$SRC" /tmp/opencsw-total-probe/
} >"$LOG" 2>&1 || {
  printf '{"status":"error","updated_at":"%s","source":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SRC" >"$TMP"
  mv "$TMP" "$OUT"
  exit 1
}

total_bytes=$(awk -F: '/Total file size:/ {
  if (match($2, /[0-9][0-9,]*/)) {
    value = substr($2, RSTART, RLENGTH)
    gsub(/,/, "", value)
    print value
    exit
  }
}' "$LOG")
file_count=$(awk -F: '/Number of files:/ {
  if (match($2, /[0-9][0-9,]*/)) {
    value = substr($2, RSTART, RLENGTH)
    gsub(/,/, "", value)
    print value
    exit
  }
}' "$LOG")

cat >"$TMP" <<EOF_JSON
{"status":"ok","updated_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","source":"$SRC","total_bytes":${total_bytes:-0},"file_count":${file_count:-0}}
EOF_JSON
mv "$TMP" "$OUT"
