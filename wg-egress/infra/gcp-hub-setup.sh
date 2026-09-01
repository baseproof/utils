#!/usr/bin/env bash
#
# gcp-hub-setup.sh — stand up the WireGuard hub VM and its DNS record.
#
# Idempotent: re-running skips anything that already exists.
#
#   ./gcp-hub-setup.sh
#   SKIP_DNS=1 ./gcp-hub-setup.sh     # if your domain is not on Cloud DNS
#
set -euo pipefail

# ----------------------------- edit these -----------------------------------

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"

VM_NAME="${VM_NAME:-wg-hub}"
MACHINE="${MACHINE:-e2-small}"
IP_NAME="${IP_NAME:-wg-hub-ip}"

DOMAIN="${DOMAIN:-example.com}"          # your registered domain
HOSTNAME="${HOSTNAME:-hub.$DOMAIN}"      # what clients resolve
DNS_ZONE="${DNS_ZONE:-hub-zone}"         # Cloud DNS managed zone name
TTL="${TTL:-60}"

# -----------------------------------------------------------------------------

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[[ -n "$PROJECT" ]] || die "no project set: gcloud config set project YOUR_PROJECT"
[[ "$DOMAIN" != "example.com" || -n "${SKIP_DNS:-}" ]] \
  || die "set DOMAIN to your real domain, or pass SKIP_DNS=1"

say "project=$PROJECT region=$REGION zone=$ZONE"
gcloud config set project "$PROJECT" >/dev/null

# --------------------------- 1. APIs ----------------------------------------

say "Enabling APIs (no-op if already on)"
gcloud services enable compute.googleapis.com dns.googleapis.com --quiet

# --------------------------- 2. static IP ------------------------------------

# A reserved address is the difference between "the hub moved" being a planned
# migration and being an outage. Everything else here assumes it.
if gcloud compute addresses describe "$IP_NAME" --region="$REGION" >/dev/null 2>&1; then
  say "Reusing reserved address $IP_NAME"
else
  say "Reserving static IP $IP_NAME"
  gcloud compute addresses create "$IP_NAME" --region="$REGION"
fi
HUB_IP="$(gcloud compute addresses describe "$IP_NAME" --region="$REGION" --format='value(address)')"
say "hub address: $HUB_IP"

# --------------------------- 3. firewall -------------------------------------

fw() {   # name, rules, source, tag
  if gcloud compute firewall-rules describe "$1" >/dev/null 2>&1; then
    say "Firewall $1 exists"
  else
    say "Creating firewall $1 ($2 from $3)"
    gcloud compute firewall-rules create "$1" \
      --allow="$2" --source-ranges="$3" --target-tags="$4" --description="$5"
  fi
}

fw wg-hub-udp   udp:51820   0.0.0.0/0        wg-hub "WireGuard data plane"
fw wg-hub-web   tcp:80,tcp:443 0.0.0.0/0     wg-hub "enrollment API over TLS + ACME"
fw wg-hub-ssh   tcp:22      35.235.240.0/20  wg-hub "SSH via IAP tunnel only"

# Deliberately NOT opened: tcp:8080. hubd binds it, but only peers inside the
# tunnel reach it directly; the public path goes through TLS on 443.

# --------------------------- 4. startup script -------------------------------

STARTUP="$(mktemp)"
cat > "$STARTUP" <<STARTUP_EOF
#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y wireguard iptables debian-keyring debian-archive-keyring apt-transport-https curl gnupg

# Peers reach each other through the hub, which requires forwarding.
cat > /etc/sysctl.d/99-wg-hub.conf <<'SYS'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYS
sysctl --system

# WireGuard interface. hubd manages peers on top of this; it never creates it.
if [ ! -f /etc/wireguard/wg0.conf ]; then
  umask 077
  mkdir -p /etc/wireguard
  wg genkey > /etc/wireguard/private.key
  cat > /etc/wireguard/wg0.conf <<WGC
[Interface]
Address = 10.88.0.1/16
ListenPort = 51820
PrivateKey = \$(cat /etc/wireguard/private.key)
WGC
  chmod 600 /etc/wireguard/wg0.conf
fi
systemctl enable --now wg-quick@wg0

# Caddy terminates TLS for the enrollment endpoint and gets its own certificate.
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

cat > /etc/caddy/Caddyfile <<CADDY
${HOSTNAME} {
	reverse_proxy 127.0.0.1:8080
}
CADDY
systemctl restart caddy

mkdir -p /var/lib/hubd /etc/hubd
chmod 700 /etc/hubd
echo "hub host prepared" > /var/log/hub-setup.done
STARTUP_EOF

# --------------------------- 5. the VM ---------------------------------------

if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
  say "VM $VM_NAME already exists — leaving it alone"
else
  say "Creating $VM_NAME ($MACHINE, debian-12)"
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type="$MACHINE" \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-balanced \
    --address="$HUB_IP" \
    --can-ip-forward \
    --tags=wg-hub \
    --metadata-from-file=startup-script="$STARTUP" \
    --scopes=cloud-platform
fi
rm -f "$STARTUP"

# --------------------------- 5b. it can actually reach out --------------------

# The hub needs a public address in both directions: peers dial 51820 on it,
# and it dials out to install itself. A VM with no accessConfig still answers
# the metadata server, so it boots clean and looks alive on the console while
# every package fetch times out and the startup script dies at exit 100.
EXT="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
        --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
if [[ -z "$EXT" ]]; then
  die "$VM_NAME has no external IP: it cannot reach the internet and peers
    cannot reach it. Attach the reserved address, then re-run:
      gcloud compute instances add-access-config $VM_NAME --zone=$ZONE \\
        --address=$HUB_IP
    If it still cannot get out, infra/gcp-egress-check.sh explains why."
elif [[ "$EXT" != "$HUB_IP" ]]; then
  warn "$VM_NAME answers on $EXT, not the reserved $HUB_IP — DNS will be wrong"
else
  say "external address $EXT"
fi

FWD="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
        --format='value(canIpForward)')"
[[ "$FWD" == "True" ]] \
  || warn "canIpForward is off on $VM_NAME — peer-to-peer traffic will be
    dropped with no error anywhere, and it cannot be set on a running
    instance. The VM has to be recreated with --can-ip-forward."

# --------------------------- 6. DNS ------------------------------------------

if [[ -n "${SKIP_DNS:-}" ]]; then
  warn "SKIP_DNS set — create this record wherever your domain is hosted:"
  echo "    $HOSTNAME.  $TTL  IN  A  $HUB_IP"
else
  if gcloud dns managed-zones describe "$DNS_ZONE" >/dev/null 2>&1; then
    say "Reusing managed zone $DNS_ZONE"
  else
    say "Creating managed zone $DNS_ZONE for $DOMAIN"
    gcloud dns managed-zones create "$DNS_ZONE" \
      --dns-name="$DOMAIN." \
      --description="WireGuard hub discovery"
  fi

  FQDN="$HOSTNAME."
  CURRENT="$(gcloud dns record-sets list --zone="$DNS_ZONE" --name="$FQDN" \
               --type=A --format='value(rrdatas[0])' 2>/dev/null || true)"
  if [[ "$CURRENT" == "$HUB_IP" ]]; then
    say "A record already correct"
  elif [[ -n "$CURRENT" ]]; then
    say "Updating A record $CURRENT -> $HUB_IP"
    gcloud dns record-sets update "$FQDN" --zone="$DNS_ZONE" \
      --type=A --ttl="$TTL" --rrdatas="$HUB_IP"
  else
    say "Creating A record $FQDN -> $HUB_IP"
    gcloud dns record-sets create "$FQDN" --zone="$DNS_ZONE" \
      --type=A --ttl="$TTL" --rrdatas="$HUB_IP"
  fi

  say "Nameservers for $DOMAIN — set these at your registrar:"
  gcloud dns managed-zones describe "$DNS_ZONE" --format='value(nameServers)' | tr ';' '\n'
fi

# --------------------------- 7. what's left ----------------------------------

cat <<EOF

$(say "Infrastructure up.")

  hub address : $HUB_IP
  hostname    : $HOSTNAME
  ssh         : gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap

Wait for the startup script, then confirm the host is ready:
  gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap \\
    --command 'cat /var/log/hub-setup.done; sudo wg show; systemctl is-active caddy'

Build and install the control plane:
  GOOS=linux GOARCH=amd64 go build -o hubd .
  gcloud compute scp hubd $VM_NAME:~ --zone=$ZONE --tunnel-through-iap
  gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap --command '
    sudo mv ~/hubd /usr/local/bin/ && sudo chmod +x /usr/local/bin/hubd
    sudo /usr/local/bin/hubd enroll-key
    sudo /usr/local/bin/hubd serve -endpoint $HOSTNAME:51820 &'

Then mint a join token and enroll a Mac:
  sudo /usr/local/bin/hubd join-token -node macmini-garage -class egress -ttl 1h
  sudo ./wg-egress.sh enroll https://$HOSTNAME <token>

EOF
