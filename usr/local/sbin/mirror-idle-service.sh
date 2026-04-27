#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu
name="${1:-mirror-idle}"
trap 'exit 0' INT TERM
while :; do
  sleep 3600 &
  wait $! || true
done
