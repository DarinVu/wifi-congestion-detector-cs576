#!/bin/bash
# Emulated WAN-only bottleneck for the "wan_only" test condition (macOS).
#
# Adds delay and a bandwidth cap to traffic between this Mac and the EXTERNAL
# ping target only, using macOS's built-in pf + dummynet. Traffic to the router
# is untouched, so the local Wi-Fi path stays normal while the "WAN" path
# degrades. This emulates an upstream bottleneck; a router-side bandwidth limit
# (QoS) is the more realistic option if your router has one.
#
# Usage (needs sudo, changes firewall state until you turn it off):
#   sudo experiment/wan_shaper.sh on      # start shaping
#   sudo experiment/wan_shaper.sh off     # stop shaping and restore pf
#   sudo experiment/wan_shaper.sh status
#
# Settings (env vars): TARGET=1.1.1.1 DELAY_MS=150 BW=2Mbit/s
#   e.g. sudo TARGET=8.8.8.8 DELAY_MS=200 experiment/wan_shaper.sh on

set -euo pipefail

TARGET="${TARGET:-1.1.1.1}"
DELAY_MS="${DELAY_MS:-150}"
BW="${BW:-2Mbit/s}"
ANCHOR="wifi_cd_wanshaper"
PIPE=4242
TOKEN_FILE="/var/run/${ANCHOR}.token"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ${1:-on}" >&2
  exit 1
fi

case "${1:-}" in
  on)
    dnctl pipe "$PIPE" config delay "$DELAY_MS" bw "$BW"
    # Load Apple's default ruleset plus our anchors (default /etc/pf.conf is unchanged on disk).
    (cat /etc/pf.conf; echo "dummynet-anchor \"$ANCHOR\""; echo "anchor \"$ANCHOR\"") \
      | pfctl -q -f - 2>/dev/null
    printf 'dummynet out quick proto { icmp, tcp, udp } from any to %s pipe %s\n' "$TARGET" "$PIPE" \
      | pfctl -q -a "$ANCHOR" -f -
    # Enable pf with a reference token so "off" only drops our reference.
    pfctl -E 2>&1 | awk '/Token/ {print $NF}' > "$TOKEN_FILE"
    echo "WAN shaper ON: +${DELAY_MS} ms delay, ${BW} cap for traffic to ${TARGET}"
    echo "Check: ping -c 5 ${TARGET}   (should be about ${DELAY_MS} ms higher than usual)"
    ;;
  off)
    pfctl -q -a "$ANCHOR" -F all 2>/dev/null || true
    dnctl -q pipe delete "$PIPE" 2>/dev/null || true
    pfctl -q -f /etc/pf.conf 2>/dev/null || true
    if [[ -s "$TOKEN_FILE" ]]; then
      pfctl -X "$(cat "$TOKEN_FILE")" >/dev/null 2>&1 || true
      rm -f "$TOKEN_FILE"
    fi
    echo "WAN shaper OFF (pf rules restored from /etc/pf.conf)"
    ;;
  status)
    echo "Anchor rules:"; pfctl -a "$ANCHOR" -s rules 2>/dev/null || true
    echo "Pipe:";         dnctl pipe show "$PIPE" 2>/dev/null || echo "  (none)"
    ;;
  *)
    sed -n '2,17p' "$0"
    exit 1
    ;;
esac
