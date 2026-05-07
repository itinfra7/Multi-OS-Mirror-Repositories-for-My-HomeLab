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
printf 'mirror=%s/opencsw/current/unstable/\n' "$PUBLIC_URL" > /storage/opencsw/CLIENT_URL.txt
openbsd_versions() {
  find /storage/openbsd -maxdepth 1 -type d -name '[0-9].[0-9]' -exec basename {} \; 2>/dev/null \
    | sort -t. -k1,1n -k2,2n
}
if [ -f /storage/openbsd/.mirror-state ]; then
  latest=$(awk -F= '/^LATEST=/{print $2}' /storage/openbsd/.mirror-state)
  previous=$(awk -F= '/^PREVIOUS=/{print $2}' /storage/openbsd/.mirror-state)
else
  latest=$(openbsd_versions | tail -1)
  previous=$(openbsd_versions | tail -2 | head -1)
fi
if [ -n "${latest:-}" ] && [ -d "/storage/openbsd/$latest/amd64" ]; then
  ready=$latest
else
  ready=${previous:-${latest:-}}
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
    value = json.load(open('/storage/omnios/.mirror-state.json')).get('latest')
    if value:
        print(value)
except Exception:
    pass
PY
)
else
  omnios_latest=$(find /storage/omnios -maxdepth 1 -type d -name 'r151[0-9][0-9][0-9]' -exec basename {} \; 2>/dev/null | sort -r | head -1)
fi
omnios_latest=${omnios_latest:-latest-lts}
cat > /storage/omnios/CLIENT_URLS.txt <<HINT
# OmniOS IPS publisher examples for this internal mirror.
# latest-lts currently points to $omnios_latest.
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/omnios/latest-lts/core omnios
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/omnios/latest-lts/extra extra.omnios
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/omnios/localhostomnios localhostomnios
pkg refresh --full
HINT
mkdir -p /storage/openindiana
cat > /storage/openindiana/CLIENT_URLS.txt <<HINT
# OpenIndiana Hipster IPS publisher examples for this internal mirror.
# Hipster is rolling; upstream does not expose separate latest/previous IPS repositories.
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/openindiana/hipster openindiana.org
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/openindiana/hipster-encumbered hipster-encumbered
pkg set-publisher -G '*' -M '*' -g ${PUBLIC_URL}/openindiana/localhostoih localhostoih
pkg refresh --full
HINT
