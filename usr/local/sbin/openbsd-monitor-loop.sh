#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu
while :; do
  /usr/local/sbin/openbsd-monitor-collect.py >/dev/null 2>&1 || true
  sleep 5
done
