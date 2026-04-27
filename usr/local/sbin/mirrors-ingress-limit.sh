#!/bin/sh
# Credits: itinfra7 from GitHub
set -eu

CONF="/etc/conf.d/mirrors-ingress-limit"
[ -r "$CONF" ] && . "$CONF"

IFACE="${MIRRORS_LIMIT_IFACE:-eth0}"
RATE="${MIRRORS_INGRESS_RATE:-16mbit}"
BURST="${MIRRORS_INGRESS_BURST:-512k}"
ENABLED="${MIRRORS_LIMIT_ENABLED:-yes}"
MODE="${1:-start}"

write_conf() {
  enabled="$1"
  tmp="${CONF}.tmp"
  mkdir -p "$(dirname "$CONF")"
  cat >"$tmp" <<EOF_CONF
MIRRORS_LIMIT_ENABLED="$enabled"
MIRRORS_LIMIT_IFACE="$IFACE"
MIRRORS_INGRESS_RATE="$RATE"
MIRRORS_INGRESS_BURST="$BURST"
EOF_CONF
  mv "$tmp" "$CONF"
}

apply_limit() {
  tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
  tc qdisc add dev "$IFACE" handle ffff: ingress
  tc filter add dev "$IFACE" parent ffff: protocol ip prio 10 \
    u32 match u32 0 0 police rate "$RATE" burst "$BURST" conform-exceed drop flowid :1
  if ip -6 route show default >/dev/null 2>&1 && ip -6 route show default | grep -q .; then
    tc filter add dev "$IFACE" parent ffff: protocol ipv6 prio 10 \
      u32 match u32 0 0 police rate "$RATE" burst "$BURST" conform-exceed drop flowid :1 || true
  fi
}

clear_limit() {
  tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
}

is_active() {
  tc filter show dev "$IFACE" parent ffff: 2>/dev/null | grep -q 'police .*rate'
}

case "$MODE" in
  start)
    case "$ENABLED" in
      yes|true|1|on) apply_limit ;;
      *) clear_limit ;;
    esac
    ;;
  apply)
    apply_limit
    ;;
  enable)
    write_conf yes
    apply_limit
    ;;
  disable)
    write_conf no
    clear_limit
    ;;
  toggle)
    if is_active; then
      write_conf no
      clear_limit
    else
      write_conf yes
      apply_limit
    fi
    ;;
  clear|stop)
    clear_limit
    ;;
  status)
    printf 'configured=%s iface=%s rate=%s burst=%s\n' "$ENABLED" "$IFACE" "$RATE" "$BURST"
    tc qdisc show dev "$IFACE"
    tc filter show dev "$IFACE" parent ffff: 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 {start|apply|enable|disable|toggle|clear|status}" >&2
    exit 2
    ;;
esac
