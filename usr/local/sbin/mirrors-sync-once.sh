#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu
LOCK=/run/lock/mirrors-sync-sequence.lock
LOG=/storage/logs/mirrors-sync-sequence.log
LOG_DIR=/storage/logs
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
PIDS_FILE="/tmp/mirrors-sync-parallel.$$.pids"
mkdir -p /run/lock "$LOG_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  printf '%s|already-running\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
  exit 0
fi

cleanup() {
  rm -f "$PIDS_FILE"
}
trap cleanup EXIT

run_step_background() {
  name="$1"
  shift
  step_log="$LOG_DIR/mirrors-sync-${name}.log"
  (
    printf '%s|start|%s|run=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$RUN_ID"
    if "$@"; then
      rc=0
    else
      rc=$?
    fi
    printf '%s|end|%s|run=%s|rc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$RUN_ID" "$rc"
    exit "$rc"
  ) >>"$step_log" 2>&1 &
  pid=$!
  printf '%s %s\n' "$name" "$pid" >>"$PIDS_FILE"
  printf '%s|launch|%s|run=%s|pid=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$RUN_ID" "$pid" >>"$LOG"
}

printf '%s|parallel-start|run=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" >>"$LOG"
run_step_background alpine nice -n 10 /usr/local/bin/alpine-mirror-sync.sh
run_step_background opencsw nice -n 10 /usr/local/sbin/sync-opencsw-full.sh
run_step_background openbsd nice -n 10 /usr/local/sbin/sync-openbsd-amd64.sh
run_step_background omnios nice -n 10 /usr/local/sbin/sync-omnios-lts.py
run_step_background openindiana nice -n 10 /usr/local/sbin/sync-openindiana-hipster.py

overall_rc=0
while read -r name pid; do
  if wait "$pid"; then
    rc=0
  else
    rc=$?
    overall_rc=1
  fi
  printf '%s|joined|%s|run=%s|pid=%s|rc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$RUN_ID" "$pid" "$rc" >>"$LOG"
done <"$PIDS_FILE"
printf '%s|parallel-end|run=%s|rc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" "$overall_rc" >>"$LOG"
exit "$overall_rc"
