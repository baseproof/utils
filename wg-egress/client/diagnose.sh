#!/usr/bin/env bash
#
# diagnose.sh — check every layer of the egress node, top to bottom.
#
#   sudo bash diagnose.sh
#
# Every check has a timeout and nothing here changes state, so it is safe to
# run against a live node at any time.
#
# shellcheck disable=SC2015
# (A && B || C is used throughout; ok/no/hm always return 0, so C never runs
#  spuriously. Deliberately no -e: a failing check must not stop the run.)
set -uo pipefail

CONF_DIR="/usr/local/etc/wg-egress"
LOG_DIR="/var/log/wg-egress"
LABEL="com.local.wg-egress"
IFACE="wg0"
PLIST="/Library/LaunchDaemons/$LABEL.plist"

if [[ -n "${BREW_PREFIX:-}" ]]; then BREW="$BREW_PREFIX"
elif [[ -x /opt/homebrew/bin/brew ]]; then BREW=/opt/homebrew
elif [[ -x /usr/local/bin/brew ]]; then BREW=/usr/local
else BREW=""; fi
export PATH="${BREW:+$BREW/bin:$BREW/sbin:}/usr/bin:/bin:/usr/sbin:/sbin"

pass=0; fail=0; warn=0
ok()   { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; pass=$((pass+1)); }
no()   { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }
hm()   { printf '  \033[1;33mWARN\033[0m %s\n' "$*"; warn=$((warn+1)); }
hint() { printf '       \033[2m%s\033[0m\n' "$*"; }
sect() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# ---------------------------------------------------------------- 1. tooling

sect "tools"
[[ -n "$BREW" ]] && ok "homebrew prefix $BREW" || no "homebrew not found"
for t in wg wg-quick wireguard-go tinyproxy jq nc; do
  p="$(command -v "$t" 2>/dev/null)"
  [[ -n "$p" ]] && ok "$t ($p)" || { no "$t missing"; hint "brew install ${t/wireguard-go/wireguard-go}"; }
done

# ---------------------------------------------------------------- 2. files

sect "configuration"
for f in "$CONF_DIR/identity.json" "$CONF_DIR/private.key" \
         "$BREW/etc/wireguard/$IFACE.conf" "$BREW/etc/tinyproxy/tinyproxy.conf" "$PLIST"; do
  if [[ -s "$f" ]]; then
    ok "$(printf '%-46s %s' "$f" "$(stat -f '%Sp %Su' "$f" 2>/dev/null)")"
  else
    no "$f missing or empty"
  fi
done

ID="$CONF_DIR/identity.json"
if [[ -s "$ID" ]]; then
  NODE=$(jq -r '.client_id // empty'      "$ID" 2>/dev/null)
  TUN=$(jq  -r '.tunnel_ip // empty'      "$ID" 2>/dev/null)
  CIDR=$(jq -r '.tunnel_cidr // empty'    "$ID" 2>/dev/null)
  PORT=$(jq -r '.proxy_port // 8888'      "$ID" 2>/dev/null)
  HUBEP=$(jq -r '.server_endpoint // empty' "$ID" 2>/dev/null)
  HUBPK=$(jq -r '.server_public_key // empty' "$ID" 2>/dev/null)
  TOK=$(jq  -r '.auth_token // empty'     "$ID" 2>/dev/null)
  HUBIP="${CIDR%%/*}"; HUBIP="${HUBIP%.*}.1"
  ok "identity: $NODE at $TUN, hub $HUBEP"
else
  no "not enrolled — run: wg-egress.sh enroll <url> <token>"
  exit 1
fi

# ---------------------------------------------------------------- 3. daemon

sect "launch daemon"
if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  state=$(launchctl print "system/$LABEL" 2>/dev/null | awk '/^\tstate = /{print $3}')
  pid=$(launchctl print "system/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}')
  last=$(launchctl print "system/$LABEL" 2>/dev/null | awk '/last exit code = /{print $NF}')
  [[ "$state" == "running" ]] && ok "daemon running (pid ${pid:-?})" \
                              || no "daemon state=${state:-unknown} last exit=${last:-?}"
  [[ "${last:-0}" == "0" || -z "${last:-}" ]] || hint "non-zero exit — see the log section below"
else
  no "daemon not loaded"
  hint "sudo launchctl bootstrap system $PLIST"
fi

# ---------------------------------------------------------------- 4. tunnel

sect "wireguard"
UTUN=$(cat "/var/run/wireguard/$IFACE.name" 2>/dev/null || true)
if [[ -z "$UTUN" ]]; then
  no "interface not up (no /var/run/wireguard/$IFACE.name)"
  hint "sudo wg-quick up $IFACE   # run it by hand to see the error"
else
  ok "interface $UTUN"
  if ifconfig "$UTUN" 2>/dev/null | grep -q "$TUN"; then
    ok "address $TUN assigned"
  else
    no "address $TUN not on $UTUN"
  fi

  HS=$(wg show "$UTUN" latest-handshakes 2>/dev/null | awk 'NR==1{print $2}')
  if [[ -n "${HS:-}" && "$HS" != "0" ]]; then
    AGE=$(( $(date +%s) - HS ))
    if (( AGE < 200 )); then ok "handshake ${AGE}s ago"
    else no "handshake stale (${AGE}s) — hub unreachable or key mismatch"; fi
  else
    no "no handshake ever completed"
    hint "check the hub still holds this peer: sudo wg show   (on the hub)"
  fi

  PEERPK=$(wg show "$UTUN" peers 2>/dev/null | head -1)
  if [[ "$PEERPK" == "$HUBPK" ]]; then ok "peer key matches enrollment"
  else no "peer key mismatch"; hint "configured: ${PEERPK:-none}"; hint "enrolled:   $HUBPK"; fi

  route -n get -net "${CIDR%%/*}" 2>/dev/null | grep -q "$UTUN" \
    && ok "route for $CIDR via $UTUN" || hm "no route for $CIDR via $UTUN"
fi

# ---------------------------------------------------------------- 5. proxy

sect "tinyproxy"
if pgrep -x tinyproxy >/dev/null 2>&1; then ok "process running"
else no "not running"; hint "tinyproxy -c $BREW/etc/tinyproxy/tinyproxy.conf"; fi

if nc -z -G 3 -w 3 "$TUN" "$PORT" 2>/dev/null; then
  ok "listening on $TUN:$PORT"
  EGRESS=$(curl -s -m 15 -x "http://$TUN:$PORT" https://api.ipify.org 2>/dev/null || true)
  if [[ -n "$EGRESS" ]]; then ok "proxy egress IP: $EGRESS  (should be your home IP)"
  else no "proxy accepted the connection but fetched nothing"; fi
else
  no "nothing listening on $TUN:$PORT"
  hint "tinyproxy binds the tunnel address, so it needs $UTUN up first"
fi

# ---------------------------------------------------------------- 6. hub

sect "hub"
HUBHOST="${HUBEP%%:*}"
A=$(dig +short +time=3 +tries=1 A "$HUBHOST" 2>/dev/null | head -1)
[[ -n "$A" ]] && ok "$HUBHOST resolves to $A" || no "$HUBHOST does not resolve"

TXT=$(dig +short +time=3 +tries=1 TXT "_hub.${HUBHOST#*.}" 2>/dev/null | head -1)
if [[ -n "$TXT" ]]; then
  ok "TXT: $TXT"
  case "$TXT" in
    *"$HUBPK"*) ok "TXT key matches the hub we enrolled with" ;;
    *) no "TXT key is stale — hubfind would fail over to a hub that no longer exists"
       hint "update _hub.${HUBHOST#*.} with pk=$HUBPK" ;;
  esac
else
  hm "no TXT discovery record"
fi

CODE=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$HUBHOST/healthz" 2>/dev/null || echo 000)
[[ "$CODE" == "200" ]] && ok "https://$HUBHOST/healthz -> 200" || no "healthz -> $CODE"

if [[ -n "$UTUN" ]]; then
  if ping -c1 -t3 "$HUBIP" >/dev/null 2>&1; then ok "hub $HUBIP reachable through the tunnel"
  else no "hub $HUBIP unreachable through the tunnel"; fi

  PEERS=$(curl -s -m 10 -H "authorization: Bearer $TOK" \
          "http://$HUBIP:8080/v1/peers?class=egress&healthy=true" 2>/dev/null || true)
  if [[ -n "$PEERS" ]]; then
    N=$(jq -r '.count // 0' <<<"$PEERS" 2>/dev/null || echo 0)
    grep -q "$NODE" <<<"$PEERS" && ok "control plane lists $NODE as healthy ($N total)" \
                                || no "control plane does not list $NODE as healthy"
  else
    no "control plane unreachable at $HUBIP:8080"
  fi
fi

# ---------------------------------------------------------------- 7. log

sect "recent daemon log"
if [[ -s "$LOG_DIR/daemon.log" ]]; then
  tail -n 12 "$LOG_DIR/daemon.log" | sed 's/^/  /'
else
  echo "  (empty)"
fi

# ---------------------------------------------------------------- summary

printf '\n\033[1m%d passed, %d failed, %d warnings\033[0m\n' "$pass" "$fail" "$warn"
(( fail == 0 )) && printf 'Node is healthy.\n' || printf 'Fix the FAIL lines top-down; later checks depend on earlier ones.\n'
exit $(( fail > 0 ))
