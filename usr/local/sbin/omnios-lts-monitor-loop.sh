#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

stop_loop() {
  exit 0
}

trap stop_loop INT TERM

while :; do
  flock -n /run/lock/omnios-lts-monitor-loop.lock /usr/local/sbin/omnios-lts-monitor-collect.py >/dev/null 2>&1 || true
  sleep 5
done
