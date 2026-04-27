#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

LOCK=/run/lock/opencsw-monitor-loop.lock
mkdir -p /run/lock

while :; do
  flock -n "$LOCK" /usr/local/sbin/opencsw-monitor-collect.py >/dev/null 2>&1 || true
  sleep 5
done
