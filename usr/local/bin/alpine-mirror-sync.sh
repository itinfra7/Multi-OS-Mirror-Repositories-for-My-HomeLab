#!/bin/sh
# Credits: itinfra7 from GitHub
#
# Alpine Linux Repository Mirror Sync Script
#
# Mirrors (x86_64 only):
#   - edge:          main, community, releases, testing
#   - latest-stable: main, community, releases
#   - prev-stable:   main, community, releases
#
# On new release: latest-stable shifts forward, old prev-stable is deleted.
#

REPO_BASE="/alpine-repo"
RSYNC_MIRROR="rsync://rsync.alpinelinux.org/alpine"
LOCK_FILE="/var/run/alpine-mirror-sync.lock"
LOG_FILE="/var/log/alpine-mirror-sync.log"
STATE_FILE="${REPO_BASE}/.mirror-state"
PROGRESS_FILE="/tmp/.mirror-progress"
RSYNC_PROGRESS="/tmp/.mirror-rsync-out"
FILES_DONE_FILE="/tmp/.mirror-files-done"
JOB_DONE=0
JOB_TOTAL=10

# ── Logging ──────────────────────────────────────────────
log() {
    local msg
    msg="$(date '+%Y-%m-%d %H:%M:%S') [$$] $1"
    echo "$msg" >> "$LOG_FILE"
    # Also print to stderr so interactive users can see progress
    echo "$msg" >&2
}

# ── Lock ─────────────────────────────────────────────────
acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local old_pid
        old_pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            log "ERROR: Already running (PID $old_pid)"
            exit 1
        fi
        log "WARN: Removing stale lock file"
        rm -f "$LOCK_FILE"
    fi
    echo $$ > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT INT TERM
}

# ── Resolve latest-stable symlink from upstream ──────────
resolve_latest_stable() {
    rsync --no-motd --list-only -l "${RSYNC_MIRROR}/" 2>/dev/null \
        | grep 'latest-stable' \
        | sed 's/.*-> //' \
        | tr -d ' /'
}

# ── List all vX.Y directories from upstream (sorted) ────
list_upstream_versions() {
    rsync --no-motd --list-only "${RSYNC_MIRROR}/" 2>/dev/null \
        | awk '{print $NF}' \
        | grep '^v[0-9]' \
        | sed 's|/$||' \
        | sort -t. -k1.2,1n -k2,2n
}

# ── Update progress file ─────────────────────────────────
update_progress() {
    echo "${JOB_DONE} ${JOB_TOTAL} $1" > "$PROGRESS_FILE"
}

# ── Sync one repo/arch combination ──────────────────────
sync_repo() {
    local ver="$1" repo="$2"
    local src="${RSYNC_MIRROR}/${ver}/${repo}/x86_64/"
    local dst="${REPO_BASE}/${ver}/${repo}/x86_64/"

    mkdir -p "$dst"
    log "  Syncing ${ver}/${repo}/x86_64 ..."

    # Update progress tracking
    update_progress "${ver}/${repo}/x86_64"
    : > "$RSYNC_PROGRESS"

    rsync -az --delete \
        --timeout=600 --contimeout=60 --no-motd \
        --info=progress2 --no-inc-recursive \
        --outbuf=line \
        "$src" "$dst" > "$RSYNC_PROGRESS" 2>> "$LOG_FILE"
    local rc=$?

    if [ $rc -eq 0 ]; then
        JOB_DONE=$((JOB_DONE + 1))
        # Count files in this repo and accumulate
        local this_files
        this_files=$(find "$dst" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [ -f "$FILES_DONE_FILE" ]; then
            local prev_files prev_total
            prev_files=$(awk '{print $1}' "$FILES_DONE_FILE")
            prev_total=$(awk '{print $2}' "$FILES_DONE_FILE")
            echo "$((prev_files + this_files)) $((prev_total + this_files))" > "$FILES_DONE_FILE"
        else
            echo "${this_files} ${this_files}" > "$FILES_DONE_FILE"
        fi
        update_progress "${ver}/${repo}/x86_64"
        log "  [OK]  ${ver}/${repo}/x86_64 (${this_files} files)"
    else
        log "  [FAIL] ${ver}/${repo}/x86_64 (rc=$rc)"
    fi
    return $rc
}

# ── Sync the last-updated timestamp from upstream ────────
sync_metadata() {
    rsync --no-motd --timeout=60 --contimeout=30 \
        "${RSYNC_MIRROR}/last-updated" \
        "${REPO_BASE}/last-updated" >> "$LOG_FILE" 2>&1
    rsync --no-motd --timeout=60 --contimeout=30 \
        "${RSYNC_MIRROR}/MIRRORS.txt" \
        "${REPO_BASE}/MIRRORS.txt" >> "$LOG_FILE" 2>&1
}

# ── MAIN ─────────────────────────────────────────────────
main() {
    acquire_lock

    log "============================================"
    log "Alpine Mirror Sync START"
    log "============================================"

    # ---- Determine versions ----
    log "Resolving upstream latest-stable ..."
    LATEST_STABLE=$(resolve_latest_stable)

    if [ -z "$LATEST_STABLE" ]; then
        log "Symlink resolution failed, fallback: pick highest version"
        LATEST_STABLE=$(list_upstream_versions | tail -1)
    fi

    if [ -z "$LATEST_STABLE" ]; then
        log "FATAL: Cannot determine latest-stable"
        exit 1
    fi
    log "Latest stable : $LATEST_STABLE"

    # Previous stable = one version before latest in the sorted list
    ALL_VERSIONS=$(list_upstream_versions)
    PREV_STABLE=$(echo "$ALL_VERSIONS" | grep -B1 "^${LATEST_STABLE}$" | head -1)

    if [ "$PREV_STABLE" = "$LATEST_STABLE" ] || [ -z "$PREV_STABLE" ]; then
        PREV_STABLE=""
    fi
    log "Previous stable: ${PREV_STABLE:-none}"

    # ---- Progress tracking ----
    JOB_DONE=0
    JOB_TOTAL=7
    [ -n "$PREV_STABLE" ] && JOB_TOTAL=10
    : > "$RSYNC_PROGRESS"
    echo "0 0" > "$FILES_DONE_FILE"
    update_progress "initializing"

    # ---- Sync edge (main, community, releases, testing) ----
    log "---- edge ----"
    for repo in main community releases testing; do
        sync_repo "edge" "$repo"
    done

    # ---- Sync latest-stable (main, community, releases) ----
    log "---- $LATEST_STABLE (latest-stable) ----"
    for repo in main community releases; do
        sync_repo "$LATEST_STABLE" "$repo"
    done

    # ---- Sync previous stable (main, community, releases) ----
    if [ -n "$PREV_STABLE" ]; then
        log "---- $PREV_STABLE (previous stable) ----"
        for repo in main community releases; do
            sync_repo "$PREV_STABLE" "$repo"
        done
    fi

    # ---- Metadata ----
    log "---- metadata ----"
    sync_metadata

    # ---- Symlink latest-stable ----
    rm -f "${REPO_BASE}/latest-stable"
    ln -sf "$LATEST_STABLE" "${REPO_BASE}/latest-stable"
    log "Symlink: latest-stable -> $LATEST_STABLE"

    # ---- Cleanup: remove any version not latest/prev/edge ----
    log "---- cleanup ----"
    for dir in "${REPO_BASE}"/v*/; do
        [ -d "$dir" ] || continue
        ver=$(basename "$dir")
        if [ "$ver" != "$LATEST_STABLE" ] && [ "$ver" != "$PREV_STABLE" ]; then
            log "Removing old version: $ver"
            rm -rf "$dir"
        fi
    done

    # ---- Save state ----
    cat > "$STATE_FILE" <<EOF
LATEST=$LATEST_STABLE
PREV=${PREV_STABLE:-none}
LAST_SYNC=$(date '+%Y-%m-%d %H:%M:%S %Z')
EOF

    log "============================================"
    log "Alpine Mirror Sync COMPLETE"
    log "  Disk usage: $(du -sh "$REPO_BASE" 2>/dev/null | awk '{print $1}')"
    log "============================================"
}

main "$@"
