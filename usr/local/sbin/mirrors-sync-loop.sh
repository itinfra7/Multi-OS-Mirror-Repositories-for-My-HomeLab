#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu
INTERVAL_SECONDS="${MIRRORS_SYNC_INTERVAL_SECONDS:-21600}"
trap 'exit 0' INT TERM
while :; do
  /usr/local/sbin/mirrors-sync-once.sh || true
  sleep "$INTERVAL_SECONDS" &
  wait $! || true
done
