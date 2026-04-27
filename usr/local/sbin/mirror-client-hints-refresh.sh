#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu
PUBLIC_URL="$(${MIRRORS_PUBLIC_URL_HELPER:-/usr/local/sbin/mirrors-public-url.sh})"
PUBLIC_HOST="${PUBLIC_URL#http://}"
PUBLIC_HOST="${PUBLIC_HOST#https://}"

cat > /storage/alpine/CLIENT_URLS.txt <<HINT
# /etc/apk/repositories examples for this internal mirror.
${PUBLIC_URL}/alpine/latest-stable/main
${PUBLIC_URL}/alpine/latest-stable/community
${PUBLIC_URL}/alpine/edge/main
${PUBLIC_URL}/alpine/edge/community
${PUBLIC_URL}/alpine/edge/testing
HINT
printf 'mirror=%s/opencsw/current/\n' "$PUBLIC_URL" > /storage/opencsw/CLIENT_URL.txt
if [ -f /storage/openbsd/.mirror-state ]; then
  latest=$(awk -F= '/^LATEST=/{print $2}' /storage/openbsd/.mirror-state)
  previous=$(awk -F= '/^PREVIOUS=/{print $2}' /storage/openbsd/.mirror-state)
else
  latest=7.8
  previous=7.7
fi
if [ -n "${latest:-}" ] && [ -d "/storage/openbsd/$latest/amd64" ]; then
  ready=$latest
else
  ready=${previous:-7.7}
fi
cat > /storage/openbsd/CLIENT_URLS.txt <<HINT
# OpenBSD install/upgrade mirror examples.
# Server: ${PUBLIC_HOST}
# Server directory for installer: /openbsd
${PUBLIC_URL}/openbsd/$ready/amd64/
${PUBLIC_URL}/openbsd/$ready/packages/amd64/
HINT
if [ -f /storage/omnios/.mirror-state.json ]; then
  omnios_latest=$(python3 - <<'PY'
import json
try:
    print(json.load(open('/storage/omnios/.mirror-state.json')).get('latest') or 'r151054')
except Exception:
    print('r151054')
PY
)
else
  omnios_latest=r151054
fi
cat > /storage/omnios/CLIENT_URLS.txt <<HINT
# OmniOS IPS publisher examples for this internal mirror.
# latest-lts currently points to $omnios_latest.
pkg set-publisher -g ${PUBLIC_URL}/omnios/latest-lts/core omnios
pkg set-publisher -g ${PUBLIC_URL}/omnios/latest-lts/extra extra.omnios
pkg set-publisher -O ${PUBLIC_URL}/omnios/latest-lts/core omnios
pkg set-publisher -O ${PUBLIC_URL}/omnios/latest-lts/extra extra.omnios
HINT
mkdir -p /storage/openindiana
cat > /storage/openindiana/CLIENT_URLS.txt <<HINT
# OpenIndiana Hipster IPS publisher examples for this internal mirror.
# Hipster is rolling; upstream does not expose separate latest/previous IPS repositories.
pkg set-publisher -G '*' -g ${PUBLIC_URL}/openindiana/hipster openindiana.org
pkg set-publisher -G '*' -g ${PUBLIC_URL}/openindiana/hipster-encumbered hipster-encumbered
pkg set-publisher -G '*' -g ${PUBLIC_URL}/openindiana/localhostoih localhostoih
pkg refresh --full
HINT
