#!/usr/bin/env bash
#
# gcp-egress-check.sh — why can't the hub reach the internet?
#
# Read-only. Symptom this exists for: DNS resolves, the metadata server
# answers, the VM boots clean, and every connection to a public address times
# out — so apt dies at `Failed to fetch`, the startup script exits 100, and the
# hub is left unprovisioned while the console says nothing is wrong.
#
# `gcloud compute firewall-rules list` cannot settle that question. It omits
# the implied allow-egress rule, and it omits firewall *policies* — the
# hierarchical ones on the org or folder, and the network ones attached to the
# VPC. An egress deny in either is invisible to it. This checks the whole path.
#
#   ./gcp-egress-check.sh
#   ./gcp-egress-check.sh --probe        # also curl from inside the guest
#
set -euo pipefail

command -v gcloud >/dev/null 2>&1 || {
  printf 'xx  gcloud is not on PATH\n' >&2; exit 1; }

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-wg-hub}"
REGION="${REGION:-${ZONE%-*}}"
PROBE=""
[[ "${1:-}" == "--probe" ]] && PROBE=1

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '   \033[1;32mok\033[0m   %s\n' "$*"; }
bad()  { printf '   \033[1;31mXX\033[0m   %s\n' "$*"; }
warn() { printf '   \033[1;33m!!\033[0m   %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[[ -n "$PROJECT" ]] || die "no project set: gcloud config set project YOUR_PROJECT"
gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1 \
  || die "no instance $VM_NAME in $ZONE (project $PROJECT)"

# A file, not an array: macOS ships bash 3.2, where expanding an empty array
# under `set -u` is an error rather than an empty list.
FINDINGS="$(mktemp)"
trap 'rm -f "$FINDINGS"' EXIT
finding() { printf '%s\n\n' "$1" >> "$FINDINGS"; }

field() {  # one describe field, empty string if unset
  gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
    --format="value($1)" 2>/dev/null || true
}

say "instance $VM_NAME ($ZONE, project $PROJECT)"

NETWORK="$(basename "$(field 'networkInterfaces[0].network')")"
SUBNET="$(basename "$(field 'networkInterfaces[0].subnetwork')")"
INTERNAL="$(field 'networkInterfaces[0].networkIP')"
EXTERNAL="$(field 'networkInterfaces[0].accessConfigs[0].natIP')"
FORWARD="$(field canIpForward)"
TAGS="$(field 'tags.items.list()')"
STATUS="$(field status)"

printf '   network=%s subnet=%s internal=%s status=%s\n' \
  "$NETWORK" "$SUBNET" "$INTERNAL" "$STATUS"
printf '   tags=%s canIpForward=%s\n' "${TAGS:-<none>}" "$FORWARD"

# --------------------------------------------------- 1. a path out at all

say "path to the internet"

if [[ -n "$EXTERNAL" ]]; then
  ok "external IP $EXTERNAL"
else
  bad "no external IP on nic0"
  # Cloud NAT is the only other way out. With neither, packets addressed to
  # the public internet are dropped at the VPC edge and the sender just waits.
  NAT_FOUND=""
  for router in $(gcloud compute routers list --filter="region:($REGION)" \
                    --format='value(name)' 2>/dev/null); do
    nats="$(gcloud compute routers nats list --router="$router" \
              --region="$REGION" --format='value(name)' 2>/dev/null || true)"
    if [[ -n "$nats" ]]; then
      NAT_FOUND=1
      ok "Cloud NAT on router $router: $(echo "$nats" | tr '\n' ' ')"
    fi
  done
  if [[ -z "$NAT_FOUND" ]]; then
    bad "no Cloud NAT in $REGION either"
    finding "ROOT CAUSE: $VM_NAME has no external IP, and $REGION has no Cloud NAT.
  Nothing it sends to a public address can leave the VPC — which is exactly
  the timeout apt reported. The hub needs a public address of its own anyway,
  because peers dial it on UDP 51820, so attach one rather than adding NAT:

    gcloud compute instances add-access-config $VM_NAME --zone=$ZONE \\
      --address=\$(gcloud compute addresses describe hub-ip \\
                    --region=$REGION --format='value(address)')

  Then re-run provisioning on the VM:
    sudo google_metadata_script_runner startup"
  else
    warn "NAT exists, but the hub still needs its own address for inbound 51820"
  fi
fi

# Private Google Access decides whether *.googleapis.com works without an
# external IP. The guest agent's `dial tcp 172.217.119.4:443: i/o timeout` is
# what its absence looks like, and Secret Manager fails the same way.
PGA="$(gcloud compute networks subnets describe "$SUBNET" --region="$REGION" \
        --format='value(privateIpGoogleAccess)' 2>/dev/null || true)"
printf '   subnet %s privateIpGoogleAccess=%s\n' "$SUBNET" "${PGA:-unknown}"
if [[ -z "$EXTERNAL" && "$PGA" != "True" ]]; then
  warn "the gh-token lookup will time out too — it is a googleapis.com call"
fi

# --------------------------------------------------- 2. routes

say "default route in $NETWORK"

ROUTES="$(gcloud compute routes list \
  --filter="network:($NETWORK) AND destRange=0.0.0.0/0" \
  --format='table[no-heading](name,nextHopGateway.basename(),nextHopInstance.basename(),nextHopIp,priority)' \
  2>/dev/null || true)"
if [[ -z "$ROUTES" ]]; then
  bad "no 0.0.0.0/0 route — traffic has nowhere to go"
  finding "ROOT CAUSE: network $NETWORK has no default route.
    gcloud compute routes create ${NETWORK}-default --network=$NETWORK \\
      --destination-range=0.0.0.0/0 --next-hop-gateway=default-internet-gateway"
else
  echo "$ROUTES" | sed 's/^/   /'
  # Lower priority number wins. A default route pointing at a stopped instance
  # or an ILB outranks the internet gateway and black-holes everything.
  if echo "$ROUTES" | grep -qv 'default-internet-gateway'; then
    warn "a 0.0.0.0/0 route does not point at default-internet-gateway — compare priorities"
  fi
fi

# --------------------------------------------------- 3. effective firewall

say "effective firewall (VPC rules + hierarchical and network policies)"

if ! command -v python3 >/dev/null 2>&1; then
  warn "no python3 — showing the raw table, read the EGRESS deny rows yourself"
  gcloud compute instances get-effective-firewalls "$VM_NAME" --zone="$ZONE" \
    2>/dev/null | sed 's/^/   /' || true
else
  EFF="$(gcloud compute instances get-effective-firewalls "$VM_NAME" \
          --zone="$ZONE" --format=json 2>/dev/null || echo '{}')"

  # Walked generically: the JSON key holding policy rules has been renamed
  # more than once across gcloud releases, so match on rule shape, not on key
  # name. VPC rules spell the protocol IPProtocol, policy rules ipProtocol.
  DENIES="$(printf '%s' "$EFF" | python3 -c '
import json, sys

def proto(p):
    if isinstance(p, dict):
        return str(p.get("ipProtocol") or p.get("IPProtocol") or "?")
    return str(p)

def walk(node, out):
    if isinstance(node, dict):
        if str(node.get("direction", "")).upper() == "EGRESS":
            denied = node.get("denied")
            if str(node.get("action", "")).lower().startswith("deny") or denied:
                m = node.get("match", node)
                dests = m.get("destIpRanges") or node.get("destinationRanges") or ["<any>"]
                protos = denied or m.get("layer4Configs") or [{"ipProtocol": "all"}]
                out.append("priority %-6s dest %-42s %-12s %s" % (
                    node.get("priority", "?"),
                    ",".join(dests)[:42],
                    ",".join(sorted({proto(p) for p in protos}))[:12],
                    (node.get("name") or node.get("ruleName") or
                     node.get("description") or "")[:40]))
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)

out = []
walk(json.load(sys.stdin), out)
print("\n".join(sorted(set(out))))
' 2>/dev/null || true)"

  if [[ -n "$DENIES" ]]; then
    echo "$DENIES" | sed 's/^/   /'
    # Only an unrestricted destination explains the internet timing out; an
    # RFC1918 deny breaks something else, and is worth saying so separately.
    if echo "$DENIES" | grep -qE 'dest (0\.0\.0\.0/0|<any>)'; then
      bad "an egress deny covers every destination"
      finding "ROOT CAUSE: an EGRESS deny above matches 0.0.0.0/0 and applies to this
  instance. If it lives in a firewall policy rather than a VPC rule it will
  not appear in \`gcloud compute firewall-rules list\` at all — check with:

    gcloud compute instances get-effective-firewalls $VM_NAME --zone=$ZONE

  Fix by adding a higher-priority (lower number) egress allow for this
  instance's tags, or by scoping the deny so it does not select them."
    fi
    if echo "$DENIES" | grep -qE '10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16'; then
      warn "an egress deny covers RFC1918 — the 10.88.0.0/16 tunnel is inside 10.0.0.0/8"
      finding "An EGRESS deny covers RFC1918 space. It does not explain internet
  timeouts, but 10.88.0.0/16 sits inside 10.0.0.0/8: if the hub is ever moved
  into that network, peer-to-peer traffic dies with no error on either end."
    fi
  else
    ok "no egress denies apply to this instance"
  fi
fi

# --------------------------------------------------- 4. the hub's own ports

say "hub ingress in $NETWORK"

INGRESS="$(gcloud compute firewall-rules list \
  --filter="network:($NETWORK) AND direction=INGRESS AND disabled=false" \
  --format='value[separator="  "](name, allowed.map().firewall_rule().list(), sourceRanges.list(), targetTags.list())' \
  2>/dev/null || true)"

for spec in "udp:51820|WireGuard data plane" "tcp:443|enrollment API" "tcp:22|IAP SSH"; do
  port="${spec%%|*}"; label="${spec##*|}"
  hit="$(echo "$INGRESS" | grep -F "$port" | awk '{print $1}' | tr '\n' ' ' || true)"
  if [[ -n "$hit" ]]; then ok "$label ($port): $hit"
  else bad "$label ($port): no matching ingress rule"; fi
done

if [[ "$FORWARD" != "True" ]]; then
  bad "canIpForward is off"
  finding "canIpForward is off. GCP drops any packet whose destination is not the VM
  itself, so traffic between peers vanishes with no error anywhere. It cannot
  be changed on a running instance — the VM has to be recreated with
  --can-ip-forward."
fi

# --------------------------------------------------- 5. from inside

if [[ -n "$PROBE" ]]; then
  say "probing from inside the guest (IAP)"
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --command '
    for t in https://deb.debian.org/ https://github.com/ https://secretmanager.googleapis.com/; do
      printf "   %-45s %s\n" "$t" \
        "$(curl -4 -sS -m 8 -o /dev/null -w "%{http_code}" "$t" 2>&1 | tail -c 60)"
    done
    printf "   %-45s %s\n" "metadata (link-local, always answers)" \
      "$(curl -s -m 5 -o /dev/null -w "%{http_code}" -H Metadata-Flavor:Google \
         http://169.254.169.254/computeMetadata/v1/instance/id)"
    echo
    cat /var/log/wg-egress-bootstrap.failed 2>/dev/null || true
    systemctl is-active wg-quick@wg0 caddy hubd 2>/dev/null || true' \
    || warn "probe failed — that is itself a finding if SSH normally works"
fi

# --------------------------------------------------- verdict

if [[ -s "$FINDINGS" ]]; then
  say "findings"
  sed 's/^/  /' "$FINDINGS"
  exit 1
fi

say "no blocking misconfiguration found in the control plane's view"
echo "   The path out looks correct from here. Re-run with --probe to test from"
echo "   inside the guest, and check /var/log/wg-egress-bootstrap.failed there."
