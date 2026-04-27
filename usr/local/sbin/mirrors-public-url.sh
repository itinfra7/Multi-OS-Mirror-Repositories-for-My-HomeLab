#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

CONF="${MIRRORS_SITE_CONF:-/etc/conf.d/mirrors-site}"
[ -r "$CONF" ] && . "$CONF"

normalize_url() {
  value="$1"
  case "$value" in
    http://*|https://*) printf '%s\n' "${value%/}" ;;
    *) printf '%s://%s\n' "${MIRRORS_PUBLIC_SCHEME:-http}" "${value%/}" ;;
  esac
}

detect_host() {
  ip -4 route get "${MIRRORS_ROUTE_PROBE:-1.1.1.1}" 2>/dev/null \
    | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' \
    | grep -v '^127[.]' && return 0
  hostname -f 2>/dev/null | grep -vE '^(localhost|localhost[.]localdomain)$' && return 0
  hostname 2>/dev/null | grep -v '^localhost$' && return 0
  printf '%s\n' localhost
}

if [ -n "${MIRRORS_PUBLIC_URL:-}" ]; then
  normalize_url "$MIRRORS_PUBLIC_URL"
else
  normalize_url "$(detect_host)"
fi
