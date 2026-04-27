#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

LOCK=/run/lock/opencsw-full.lock
BASE=/storage/opencsw
INCOMING="$BASE/incoming"
RELEASES="$BASE/releases"
LOGS="$BASE/logs"
STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
NEWREL="$RELEASES/$STAMP"
TMPREL="$NEWREL.tmp"
WORK="$BASE/.incoming-$STAMP"
SRC="rsync://mirror.opencsw.org/opencsw-full/"
RSYNC_LOG="$LOGS/rsync-current.log"
EVENT_LOG="$LOGS/sync-events.log"

mkdir -p /run/lock "$INCOMING" "$RELEASES" "$LOGS"
printf '%s|start|source=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SRC" >>"$EVENT_LOG"
: >"$RSYNC_LOG"

cleanup() {
  rm -rf "$WORK" "$TMPREL"
}
trap cleanup EXIT INT TERM HUP

rm -rf "$WORK" "$TMPREL"
CURRENT_REAL="$(readlink -f "$BASE/current" 2>/dev/null || true)"
LINK_DEST_ARG=
if [ -n "$CURRENT_REAL" ] && [ -d "$CURRENT_REAL" ]; then
  LINK_DEST_ARG="--link-dest=$CURRENT_REAL"
fi

RSYNC_RC=0
if [ -n "$LINK_DEST_ARG" ]; then
  if flock -n "$LOCK" rsync -aH --delete --delay-updates --timeout=1800 --outbuf=L --info=progress2,stats2,name1 "$LINK_DEST_ARG" "$SRC" "$WORK/" >"$RSYNC_LOG" 2>&1; then
    RSYNC_RC=0
  else
    RSYNC_RC=$?
  fi
else
  if flock -n "$LOCK" rsync -aH --delete --delay-updates --timeout=1800 --outbuf=L --info=progress2,stats2,name1 "$SRC" "$WORK/" >"$RSYNC_LOG" 2>&1; then
    RSYNC_RC=0
  else
    RSYNC_RC=$?
  fi
fi

if [ "$RSYNC_RC" -eq 0 ]; then
  rm -rf "$INCOMING"
  mv "$WORK" "$INCOMING"
  cp -al "$INCOMING" "$TMPREL"
  mv "$TMPREL" "$NEWREL"
  ln -sfn "$NEWREL" "$BASE/current"
  /usr/bin/find "$RELEASES" -mindepth 1 -maxdepth 1 -type d | /usr/bin/sort | /usr/bin/head -n -3 | /usr/bin/xargs -r rm -rf
  trap - EXIT INT TERM HUP
  printf '%s|end|rc=0|release=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STAMP" >>"$EVENT_LOG"
  exit 0
fi

rc=$RSYNC_RC
printf '%s|end|rc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" >>"$EVENT_LOG"
exit "$rc"
