#!/usr/bin/env bash
#
# consumer.sh — join a Linux VM to the egress network as a consumer.
#
#   sudo ./consumer.sh https://hub.baseproof.net <join-token>
#   sudo ./consumer.sh status
#
# The VM's private key is generated here and never leaves the machine. The hub
# issues identity, tunnel address and pre-shared key. The peer's class is taken
# from the signed join token, not from anything this script sends — a consumer
# cannot enrol itself as an egress node.
#
# Requires only outbound UDP to the hub — no inbound ports, so this works
# behind NAT, in a private subnet with a NAT gateway, or on a home connection.
#
set -euo pipefail

CONF_DIR="/etc/wg-egress"
IFACE="wg0"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cmd_status() {
  echo "--- identity ---"
  jq -r '{client_id,class,tunnel_ip,server_endpoint}' "$CONF_DIR/identity.json" 2>/dev/null \
    || { echo "not enrolled"; return 1; }
  echo "--- tunnel ---"
  wg show "$IFACE" 2>/dev/null || echo "interface down"
  echo "--- egress nodes ---"
  local tok hub
  tok=$(jq -r .auth_token < "$CONF_DIR/identity.json")
  hub=$(jq -r .tunnel_cidr < "$CONF_DIR/identity.json"); hub="${hub%%/*}"; hub="${hub%.*}.1"
  curl -s -m 10 -H "authorization: Bearer $tok" \
    "http://$hub:8080/v1/peers?class=egress&healthy=true" | jq -r \
    '.peers[]? | "  \(.client_id)  \(.proxy_url)  last seen \(.last_handshake_secs_ago)s ago"' \
    || echo "  control plane unreachable"
}

[[ "${1:-}" == "status" ]] && { cmd_status; exit $?; }

HUB_URL="${1:?usage: consumer.sh <hub-url> <join-token>}"
TOKEN="${2:?join token required}"

# ------------------------------------------------------------------ install

export DEBIAN_FRONTEND=noninteractive
if ! command -v wg >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq wireguard-tools curl jq
fi
mkdir -p "$CONF_DIR"; chmod 700 "$CONF_DIR"

# ------------------------------------------------------------------ enroll

PRIV="$(wg genkey)"
PUB="$(wg pubkey <<<"$PRIV")"

log "enrolling with $HUB_URL"
RESP="$(curl -fsS --max-time 20 -X POST "$HUB_URL/v1/enroll" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg t "$TOKEN" --arg k "$PUB" --arg h "$(hostname -s)" \
        '{join_token:$t, public_key:$k, hostname:$h}')")" \
  || { echo "enrollment rejected by hub" >&2; exit 1; }

umask 077
printf '%s' "$RESP" > "$CONF_DIR/identity.json"

ADDR=$(jq -r .tunnel_ip        <<<"$RESP")
CIDR=$(jq -r .tunnel_cidr      <<<"$RESP")
PSK=$(jq  -r .preshared_key    <<<"$RESP")
PUBK=$(jq -r .server_public_key<<<"$RESP")
EP=$(jq   -r .server_endpoint  <<<"$RESP")
KA=$(jq   -r .persistent_keepalive <<<"$RESP")
PORT=$(jq -r .proxy_port       <<<"$RESP")

cat > "/etc/wireguard/$IFACE.conf" <<CONF
[Interface]
PrivateKey = $PRIV
Address = $ADDR/32

[Peer]
PublicKey = $PUBK
PresharedKey = $PSK
Endpoint = $EP
AllowedIPs = $CIDR
PersistentKeepalive = $KA
CONF
chmod 600 "/etc/wireguard/$IFACE.conf"

systemctl enable --now "wg-quick@$IFACE"
log "enrolled as $(jq -r .client_id <<<"$RESP") at $ADDR"

# ------------------------------------------------------------------ verify

HUBIP="${CIDR%%/*}"; HUBIP="${HUBIP%.*}.1"
for _ in $(seq 1 15); do
  wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{exit ($2==0)}' && break
  sleep 2
done

EGRESS_JSON="$(curl -s -m 10 -H "authorization: Bearer $(jq -r .auth_token <<<"$RESP")" \
  "http://$HUBIP:8080/v1/peers?class=egress&healthy=true" || true)"
PROXY="$(jq -r '.peers[0].proxy_url // empty' <<<"$EGRESS_JSON" 2>/dev/null || true)"

cat <<EOF

$(log "done")

  tunnel address   $ADDR
  proxy port       $PORT
  healthy egress   $(jq -r '.count // 0' <<<"${EGRESS_JSON:-{\}}" 2>/dev/null)

Use it:

  export http_proxy=${PROXY:-http://10.88.0.2:$PORT}
  export https_proxy=${PROXY:-http://10.88.0.2:$PORT}
  curl https://ip.decodo.com/json?provider=IPinfo

Pick a live node at runtime instead of hardcoding one:

  curl -s -H "authorization: Bearer \$(jq -r .auth_token $CONF_DIR/identity.json)" \\
    http://$HUBIP:8080/v1/peers?class=egress\&healthy=true | jq -r '.peers[0].proxy_url'

EOF