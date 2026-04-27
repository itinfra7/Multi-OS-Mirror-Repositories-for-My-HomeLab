#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

trap 'exit 0' INT TERM
mkdir -p /run/lock

while :; do
  flock -n /run/lock/openindiana-monitor-loop.lock /usr/local/sbin/openindiana-monitor-collect.py >/dev/null 2>&1 || true
  sleep 5
done
