#!/usr/bin/env bash
#
# wg-egress — macOS egress node client.
#
#   sudo ./wg-egress.sh enroll https://hub.example.com <join-token>
#   sudo ./wg-egress.sh rotate      # new keypair, hub swaps it live
#   sudo ./wg-egress.sh status
#   sudo ./wg-egress.sh down
#
# The private key is generated here and never leaves this machine. The join
# token is an HMAC scoped to this node name; it is discarded after enrollment.
# The hub issues identity, tunnel address, and pre-shared key.
#
# Runs as a LaunchDaemon, so it comes up at boot with no user logged in —
# no auto-login required.
#
set -euo pipefail

CONF_DIR="/usr/local/etc/wg-egress"
LOG_DIR="/var/log/wg-egress"
IFACE="wg0"
LABEL="com.local.wg-egress"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
BREW="$(brew --prefix 2>/dev/null || sudo -u "${SUDO_USER:-$USER}" brew --prefix)"
export PATH="$BREW/bin:$BREW/sbin:$PATH"
mkdir -p "$CONF_DIR" "$LOG_DIR"; chmod 700 "$CONF_DIR"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# ------------------------------------------------------------------ DoH

# Resolves over DNS-over-HTTPS with the resolver's own IP pinned, so we never
# depend on the local resolver. Lets the hub move without touching this node.
doh() {
  curl -fsS --max-time 8 --resolve "$2" -H 'accept: application/dns-json' \
    "$1?name=$3&type=A" 2>/dev/null \
    | jq -r '[.Answer[]? | select(.type==1) | .data][0] // empty'
}

valid_ip() { [[ "${1:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }

# Four tiers, in order of trust. eero Secure and some Deco firmware intercept
# or block DoH; Nest Wifi forces its own resolver. So DoH is preferred but
# never load-bearing — we fall back to the router's resolver, then to the last
# address that actually produced a handshake.
resolve_host() {
  local host="$1" ip
  ip="$(doh https://cloudflare-dns.com/dns-query cloudflare-dns.com:443:1.1.1.1 "$host")"
  valid_ip "$ip" || ip="$(doh https://dns.google/resolve dns.google:443:8.8.8.8 "$host")"
  valid_ip "$ip" || ip="$(dscacheutil -q host -a name "$host" 2>/dev/null \
                          | awk '/^ip_address:/{print $2; exit}')"
  valid_ip "$ip" || ip="$(cat "$CONF_DIR/last-endpoint" 2>/dev/null)"
  valid_ip "$ip" && printf '%s' "$ip"
}

remember_endpoint() { printf '%s' "$1" > "$CONF_DIR/last-endpoint"; }

utun() { cat "/var/run/wireguard/$IFACE.name" 2>/dev/null; }

# ------------------------------------------------------------------ write configs

write_wg_conf() {   # privkey json
  local priv="$1" j="$2"
  local addr cidr psk pub ep ka
  addr=$(jq -r .tunnel_ip <<<"$j");        cidr=$(jq -r .tunnel_cidr <<<"$j")
  psk=$(jq -r .preshared_key <<<"$j");     pub=$(jq -r .server_public_key <<<"$j")
  ep=$(jq -r .server_endpoint <<<"$j");    ka=$(jq -r .persistent_keepalive <<<"$j")

  mkdir -p "$BREW/etc/wireguard"
  umask 077
  cat > "$BREW/etc/wireguard/$IFACE.conf" <<EOF
[Interface]
PrivateKey = $priv
Address = $addr/32

[Peer]
PublicKey = $pub
PresharedKey = $psk
Endpoint = $ep
AllowedIPs = $cidr
PersistentKeepalive = $ka
EOF
  chmod 600 "$BREW/etc/wireguard/$IFACE.conf"
}

write_proxy_conf() {   # tunnel_ip cidr port
  local conf="$BREW/etc/tinyproxy/tinyproxy.conf"
  mkdir -p "$(dirname "$conf")"
  cat > "$conf" <<EOF
# managed by wg-egress
Port $3
Listen $1
Allow $2
Timeout 600
MaxClients 200
LogFile "$LOG_DIR/tinyproxy.log"
PidFile "$CONF_DIR/tinyproxy.pid"
DisableViaHeader Yes
EOF
}

# ------------------------------------------------------------------ commands

# provision performs the enrollment exchange and writes both configs. Callable
# from the supervisor for unattended recovery, so it must never install the
# daemon or exit the process.
provision() {
  local hub_url="$1" token="$2"
  local priv pub
  priv="$(wg genkey)"; pub="$(wg pubkey <<<"$priv")"

  log "enrolling with $hub_url"
  local resp
  resp="$(curl -fsS --max-time 20 -X POST "$hub_url/v1/enroll" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg t "$token" --arg k "$pub" --arg h "$(scutil --get LocalHostName)" \
          '{join_token:$t, public_key:$k, hostname:$h}')")" \
    || { log "enrollment rejected by hub"; return 1; }

  umask 077
  printf '%s' "$priv" > "$CONF_DIR/private.key"; chmod 600 "$CONF_DIR/private.key"
  printf '%s' "$resp" > "$CONF_DIR/identity.json"; chmod 600 "$CONF_DIR/identity.json"

  write_wg_conf "$priv" "$resp"
  write_proxy_conf "$(jq -r .tunnel_ip <<<"$resp")" \
                   "$(jq -r .tunnel_cidr <<<"$resp")" \
                   "$(jq -r .proxy_port <<<"$resp")"

  # Kept so the node can re-enroll itself unattended. The join token is scoped
  # to this node name and class, and anyone who can read it already has the
  # private key sitting beside it — so it grants no capability they lack.
  printf '%s' "$token"   > "$CONF_DIR/join.token";  chmod 600 "$CONF_DIR/join.token"
  printf '%s' "$hub_url" > "$CONF_DIR/hub-url";     chmod 600 "$CONF_DIR/hub-url"

  if [[ "$(jq -r .reenrolled <<<"$resp")" == "true" ]]; then
    log "re-enrolled as $(jq -r .client_id <<<"$resp"), kept address $(jq -r .tunnel_ip <<<"$resp")"
  else
    log "enrolled as $(jq -r .client_id <<<"$resp") at $(jq -r .tunnel_ip <<<"$resp")"
  fi
}

cmd_enroll() {
  local hub_url="${1:?usage: enroll <hub-url> <join-token>}" token="${2:?join token required}"
  command -v wg >/dev/null || brew install wireguard-tools
  command -v tinyproxy >/dev/null || brew install tinyproxy
  command -v jq >/dev/null || brew install jq
  provision "$hub_url" "$token" || exit 1
  install_daemon
}

# rotate: generate a fresh keypair, have the hub swap it on the live device,
# then cut over locally. Address and identity are preserved.
cmd_rotate() {
  local id ctrl tok priv pub
  id="$CONF_DIR/identity.json"
  [[ -f "$id" ]] || { echo "not enrolled" >&2; exit 1; }
  ctrl="$(jq -r .control_url < "$id")"; tok="$(jq -r .auth_token < "$id")"
  priv="$(wg genkey)"; pub="$(wg pubkey <<<"$priv")"

  curl -fsS --max-time 20 -X POST "$ctrl/v1/rotate" \
    -H "authorization: Bearer $tok" -H 'content-type: application/json' \
    -d "$(jq -nc --arg k "$pub" '{public_key:$k}')" >/dev/null \
    || { echo "hub refused rotation" >&2; exit 1; }

  umask 077; printf '%s' "$priv" > "$CONF_DIR/private.key"
  write_wg_conf "$priv" "$(cat "$id")"
  wg-quick down "$IFACE" 2>/dev/null || true
  wg-quick up "$IFACE"
  log "key rotated"
}

cmd_status() {
  echo "--- identity ---"
  jq -r '{client_id,class,tunnel_ip,server_endpoint}' "$CONF_DIR/identity.json" 2>/dev/null \
    || echo "not enrolled"
  echo "--- wireguard ---"
  wg show "$(utun)" 2>/dev/null || echo "interface down"
  echo "--- proxy ---"
  local ip port
  ip="$(jq -r .tunnel_ip "$CONF_DIR/identity.json" 2>/dev/null)"
  port="$(jq -r .proxy_port "$CONF_DIR/identity.json" 2>/dev/null)"
  nc -z "$ip" "$port" 2>/dev/null && echo "listening on $ip:$port" || echo "proxy down"
  echo "--- log ---"
  tail -n 12 "$LOG_DIR/daemon.log" 2>/dev/null
}

cmd_down() {
  launchctl bootout system/"$LABEL" 2>/dev/null || true
  wg-quick down "$IFACE" 2>/dev/null || true
  pkill -f "tinyproxy -c $BREW/etc/tinyproxy" 2>/dev/null || true
  log "stopped"
}

# ------------------------------------------------------------------ supervisor

# Brings up the tunnel, starts tinyproxy once its bind address exists, and
# re-points the peer endpoint whenever the hub's DNS record changes.
cmd_run() {
  local id="$CONF_DIR/identity.json"
  local hub_host hub_port pub ip port cidr
  hub_host="$(jq -r .server_endpoint < "$id" | cut -d: -f1)"
  hub_port="$(jq -r .server_endpoint < "$id" | cut -d: -f2)"
  pub="$(jq -r .server_public_key < "$id")"
  ip="$(jq -r .tunnel_ip < "$id")"; port="$(jq -r .proxy_port < "$id")"
  cidr="$(jq -r .tunnel_cidr < "$id")"
  local hub_tunnel_ip="${cidr%%/*}"; hub_tunnel_ip="${hub_tunnel_ip%.*}.1"

  local TICK=15 STALE=200
  local last_sig="" last_tick=0 fails=0 cur_ep=""

  # A handshake older than STALE means the session is genuinely dead. This is
  # the only honest liveness signal — the interface can be up, addressed, and
  # completely unable to reach the hub.
  hs_age() {
    local u t
    u="$(utun)"; [[ -n "$u" ]] || { echo 999999; return; }
    t="$(wg show "$u" latest-handshakes 2>/dev/null | awk 'NR==1{print $2}')"
    [[ -n "$t" && "$t" != "0" ]] || { echo 999999; return; }
    echo $(( $(date +%s) - t ))
  }

  # Fingerprint of the local network. On eero/Nest/Deco a DHCP renewal, a mesh
  # node handoff, or a router reboot changes one of these three.
  netsig() {
    local r i g a
    r="$(route -n get default 2>/dev/null)"
    i="$(awk '/interface:/{print $2}' <<<"$r")"
    g="$(awk '/gateway:/{print $2}' <<<"$r")"
    a="$(ipconfig getifaddr "${i:-en0}" 2>/dev/null)"
    printf '%s|%s|%s' "${i:-none}" "${g:-none}" "${a:-none}"
  }

  # Recreates the UDP socket. Needed because a socket bound before the local
  # address changed keeps sending from a source the router will no longer route.
  rebind() {
    wg-quick down "$IFACE" 2>/dev/null || true
    wg-quick up "$IFACE" 2>/dev/null || { log "wg-quick up failed"; return 1; }
    cur_ep=""
  }

  aim() {
    local newip
    newip="$(resolve_host "$hub_host")" || return 1
    valid_ip "$newip" || return 1
    if [[ "$newip" != "$cur_ep" ]]; then
      log "hub endpoint ${cur_ep:-unset} -> $newip"
      wg set "$(utun)" peer "$pub" endpoint "$newip:$hub_port" 2>/dev/null || return 1
      cur_ep="$newip"
    fi
    return 0
  }

  # WireGuard is silent until it has something to send, so a dead session stays
  # dead until we give it a reason to handshake.
  probe() { ping -c1 -t2 "$hub_tunnel_ip" >/dev/null 2>&1 || true; }

  trap 'log "stopping"; wg-quick down "$IFACE" 2>/dev/null; exit 0' TERM INT
  rebind; aim || log "could not resolve $hub_host at startup"
  log "supervisor up: tunnel $ip, hub $hub_host:$hub_port"

  while true; do
    local now age sig
    now="$(date +%s)"

    # A wall-clock jump means the machine was asleep. Everything the socket
    # knew about the network is potentially stale.
    if (( last_tick > 0 && now - last_tick > TICK * 4 )); then
      log "clock gap $(( now - last_tick ))s — waking, rebinding"
      rebind; aim || true
    fi
    last_tick="$now"

    sig="$(netsig)"
    if [[ "$sig" != "$last_sig" ]]; then
      if [[ "$sig" == "none|none|none" ]]; then
        # Link is down. Rebinding now would just fail and cost a second
        # rebind when it returns, so record it and wait for an address.
        log "no default route — waiting for the link"
        last_sig="$sig"
      else
        [[ -n "$last_sig" ]] && { log "local network changed: $last_sig -> $sig"; rebind; }
        last_sig="$sig"
        aim || true
        probe
      fi
    fi

    age="$(hs_age)"
    if (( age <= STALE )); then
      (( fails > 0 )) && log "handshake recovered after $fails attempts (age ${age}s)"
      fails=0
      # Only cache an address that actually produced a handshake, so the
      # last-resort fallback can never pin us to a stale or poisoned answer.
      [[ -n "$cur_ep" ]] && remember_endpoint "$cur_ep"
    else
      fails=$(( fails + 1 ))
      case "$fails" in
        1|2)
          log "handshake stale (${age}s), attempt $fails: re-aiming and probing"
          aim || true; probe ;;
        3|4|5)
          log "handshake stale (${age}s), attempt $fails: rebuilding interface"
          rebind; aim || true; probe ;;
        *)
          # The hub may have been rebuilt, or this peer revoked and re-added.
          # Re-enrollment is idempotent per node name and keeps our address.
          if [[ -f "$CONF_DIR/join.token" && -f "$CONF_DIR/hub-url" ]]; then
            log "handshake dead ${age}s after $fails attempts: re-enrolling"
            if provision "$(cat "$CONF_DIR/hub-url")" "$(cat "$CONF_DIR/join.token")"; then
              pub="$(jq -r .server_public_key < "$id")"
              ip="$(jq -r .tunnel_ip < "$id")"
              rebind; aim || true; probe
              fails=0
            fi
          else
            log "handshake dead ${age}s — no join token stored, manual re-enroll needed"
          fi ;;
      esac
    fi

    # tinyproxy binds the tunnel address, not the LAN address, so DHCP churn
    # never touches it — but it does need the interface to exist first.
    if ! nc -z "$ip" "$port" 2>/dev/null; then
      ifconfig "$(utun)" 2>/dev/null | grep -q "$ip" && {
        log "starting tinyproxy on $ip:$port"
        tinyproxy -c "$BREW/etc/tinyproxy/tinyproxy.conf" 2>/dev/null || true
      }
    fi

    # Jittered backoff. Without the jitter a fleet that lost the hub together
    # would retry in lockstep and arrive as a thundering herd when it returns.
    local wait=$TICK
    (( fails > 0 )) && wait=$(( TICK * (fails < 6 ? fails : 6) ))
    sleep $(( wait + RANDOM % (wait / 2 + 1) ))
  done
}

install_daemon() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SELF</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$BREW/bin:$BREW/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/daemon.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/daemon.log</string>
</dict>
</plist>
EOF
  chown root:wheel "$PLIST"; chmod 644 "$PLIST"
  launchctl bootout system/"$LABEL" 2>/dev/null || true
  launchctl bootstrap system "$PLIST"
  pmset -a sleep 0 disablesleep 1 || true
  log "daemon installed; starts at boot with no login"
}

case "${1:-}" in
  enroll) shift; cmd_enroll "$@" ;;
  rotate) cmd_rotate ;;
  status) cmd_status ;;
  down)   cmd_down ;;
  run)    cmd_run ;;
  *) echo "usage: $0 {enroll <hub-url> <token>|rotate|status|down}" >&2; exit 2 ;;
esac
