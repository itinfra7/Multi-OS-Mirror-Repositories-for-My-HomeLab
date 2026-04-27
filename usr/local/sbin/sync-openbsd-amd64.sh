#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

BASE=/storage/openbsd
SRC="${OPENBSD_RSYNC_SOURCE:-rsync://ftp.usa.openbsd.org/ftp}"
ARCH=amd64
LOG_DIR="$BASE/logs"
RSYNC_LOG="$LOG_DIR/rsync-current.log"
EVENT_LOG="$LOG_DIR/sync-events.log"
PROGRESS_FILE=/run/openbsd-sync-progress
LOCK=/run/lock/openbsd-amd64-sync.lock
STATE_FILE="$BASE/.mirror-state"
KEEP_FILE="$BASE/.keep-versions"
PUBLIC_URL="$(${MIRRORS_PUBLIC_URL_HELPER:-/usr/local/sbin/mirrors-public-url.sh})"
PUBLIC_HOST="${PUBLIC_URL#http://}"
PUBLIC_HOST="${PUBLIC_HOST#https://}"

mkdir -p "$BASE" "$LOG_DIR" /run/lock

event() {
  printf '%s|%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$EVENT_LOG"
}

progress() {
  phase="$1"
  done="$2"
  total="$3"
  current="$4"
  message="${5:-}"
  printf '%s|%s|%s|%s|%s|%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase" "$done" "$total" "$current" "$message" >"$PROGRESS_FILE"
}

list_versions() {
  rsync --no-motd --list-only "$SRC/" 2>/dev/null \
    | awk '{print $NF}' \
    | grep -E '^[0-9]+[.][0-9]+/?$' \
    | sed 's#/$##' \
    | sort -t. -k1,1n -k2,2n
}

remote_exists() {
  rsync --no-motd --list-only "$1" >/dev/null 2>&1
}

rsync_common() {
  rsync -aH --delete --delay-updates --timeout=1800 --contimeout=60 --no-motd \
    --info=progress2,stats2,name1 --outbuf=L "$@"
}

sync_dir() {
  label="$1"
  src="$2"
  dst="$3"
  done="$4"
  total="$5"

  mkdir -p "$dst"
  : >"$RSYNC_LOG"
  progress syncing "$done" "$total" "$label" "$src"
  event "sync-start|$label|$src"
  if rsync_common "$src" "$dst" >"$RSYNC_LOG" 2>&1; then
    event "sync-ok|$label"
    return 0
  fi
  rc=$?
  event "sync-fail|$label|rc=$rc"
  return "$rc"
}

sync_optional_dir() {
  label="$1"
  src="$2"
  dst="$3"
  done="$4"
  total="$5"
  if remote_exists "$src"; then
    sync_dir "$label" "$src" "$dst" "$done" "$total"
    return $?
  fi
  rm -rf "$dst"
  event "sync-skip|$label|missing-upstream"
  progress skipped "$done" "$total" "$label" "missing upstream"
  return 0
}

sync_release_metadata() {
  version="$1"
  done="$2"
  total="$3"
  dst="$BASE/$version"
  mkdir -p "$dst"
  : >"$RSYNC_LOG"
  progress syncing "$done" "$total" "$version/release-metadata" "$SRC/$version/"
  event "sync-start|$version/release-metadata|$SRC/$version/"
  if rsync -a --timeout=300 --contimeout=60 --no-motd --outbuf=L \
      --include='/ANNOUNCEMENT' \
      --include='/README' \
      --include='/SHA256' \
      --include='/SHA256.sig' \
      --include='/root.mail' \
      --include='/openbsd-*-base.pub' \
      --exclude='*' \
      "$SRC/$version/" "$dst/" >"$RSYNC_LOG" 2>&1; then
    event "sync-ok|$version/release-metadata"
    return 0
  fi
  rc=$?
  event "sync-fail|$version/release-metadata|rc=$rc"
  return "$rc"
}

sync_top_metadata() {
  done="$1"
  total="$2"
  progress syncing "$done" "$total" top-metadata "$SRC/"
  : >"$RSYNC_LOG"
  for file in timestamp ftplist; do
    rsync -a --timeout=300 --contimeout=60 --no-motd "$SRC/$file" "$BASE/$file" >>"$RSYNC_LOG" 2>&1 || true
  done
  event "sync-ok|top-metadata"
}

cleanup_old() {
  keep_versions="$1"
  for dir in "$BASE"/[0-9].[0-9]; do
    [ -d "$dir" ] || continue
    name="$(basename "$dir")"
    echo "$keep_versions" | grep -Fxq "$name" && continue
    event "cleanup-version|$name"
    rm -rf "$dir"
  done
  for parent in "$BASE/syspatch" "$BASE/patches"; do
    [ -d "$parent" ] || continue
    for dir in "$parent"/[0-9].[0-9]; do
      [ -d "$dir" ] || continue
      name="$(basename "$dir")"
      echo "$keep_versions" | grep -Fxq "$name" && continue
      event "cleanup-aux|$parent/$name"
      rm -rf "$dir"
    done
  done
}

exec 9>"$LOCK"
if ! flock -n 9; then
  event "already-running"
  exit 0
fi

event "run-start|source=$SRC|arch=$ARCH"

versions="$(list_versions | tail -2)"
previous="$(printf '%s\n' "$versions" | head -1)"
latest="$(printf '%s\n' "$versions" | tail -1)"

if [ -z "$latest" ] || [ -z "$previous" ] || [ "$latest" = "$previous" ]; then
  event "fatal|could-not-resolve-latest-two"
  exit 1
fi

printf '%s\n' "$versions" >"$KEEP_FILE"
cat >"$STATE_FILE" <<EOF_STATE
LATEST=$latest
PREVIOUS=$previous
ARCH=$ARCH
SOURCE=$SRC
LAST_SYNC=pending
SYNC_STATUS=running
EOF_STATE
if [ -d "$BASE/$previous/$ARCH" ] && [ ! -d "$BASE/$latest/$ARCH" ]; then
  cat >"$BASE/CLIENT_URLS.txt" <<EOF_HINT
# OpenBSD install/upgrade mirror examples.
# Server: $PUBLIC_HOST
# Server directory for installer: /openbsd
# Current ready release while $latest is still syncing:
${PUBLIC_URL}/openbsd/$previous/$ARCH/
${PUBLIC_URL}/openbsd/$previous/packages/$ARCH/
# $latest URLs will be valid after this sync completes.
EOF_HINT
fi
total=$((1 + 6 * 2))
done_count=0
sync_top_metadata "$done_count" "$total"
done_count=$((done_count + 1))

for version in $versions; do
  sync_release_metadata "$version" "$done_count" "$total"
  done_count=$((done_count + 1))

  sync_dir "$version/$ARCH" "$SRC/$version/$ARCH/" "$BASE/$version/$ARCH/" "$done_count" "$total"
  done_count=$((done_count + 1))

  sync_dir "$version/packages/$ARCH" "$SRC/$version/packages/$ARCH/" "$BASE/$version/packages/$ARCH/" "$done_count" "$total"
  done_count=$((done_count + 1))

  sync_optional_dir "$version/packages-stable/$ARCH" "$SRC/$version/packages-stable/$ARCH/" "$BASE/$version/packages-stable/$ARCH/" "$done_count" "$total"
  done_count=$((done_count + 1))

  sync_optional_dir "syspatch/$version/$ARCH" "$SRC/syspatch/$version/$ARCH/" "$BASE/syspatch/$version/$ARCH/" "$done_count" "$total"
  done_count=$((done_count + 1))

  sync_optional_dir "patches/$version/common" "$SRC/patches/$version/common/" "$BASE/patches/$version/common/" "$done_count" "$total"
  done_count=$((done_count + 1))
done

cleanup_old "$versions"

cat >"$STATE_FILE" <<EOF_STATE
LATEST=$latest
PREVIOUS=$previous
ARCH=$ARCH
SOURCE=$SRC
LAST_SYNC=$(date '+%Y-%m-%d %H:%M:%S %Z')
SYNC_STATUS=complete
EOF_STATE

cat >"$BASE/CLIENT_URLS.txt" <<EOF_HINT
# OpenBSD install/upgrade mirror examples.
# Server: $PUBLIC_HOST
# Server directory for installer: /openbsd
${PUBLIC_URL}/openbsd/$latest/$ARCH/
${PUBLIC_URL}/openbsd/$latest/packages/$ARCH/
${PUBLIC_URL}/openbsd/syspatch/$latest/$ARCH/
EOF_HINT

progress complete "$total" "$total" "$latest/$previous" "sync complete"
event "run-complete|latest=$latest|previous=$previous"
