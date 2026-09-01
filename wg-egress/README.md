# wg-egress

HTTP egress for GCP-hosted RPA through Mac minis on home broadband, over
WireGuard, with a small control plane that issues identity to enrolling nodes.

```
RPA VM  ──►  WireGuard hub (GCP)  ──►  Mac mini  ──►  home ISP  ──►  internet
             10.88.0.1                 10.88.0.x
             hubd control plane        tinyproxy :8888
```

No port forwarding, no UPnP, no static IP at home. The Mac dials out, so double
NAT and carrier-grade NAT both work untouched.

## Current deployment

| Thing | Value |
| --- | --- |
| Hub VM | `wg-hub`, zone `us-central1-a`, project `legalai-460612` |
| Hub address | `34.72.191.179` (reserved as `hub-ip`) |
| Hostname | `hub.baseproof.net` |
| Discovery record | `_hub.baseproof.net` TXT |
| Hub WireGuard key | regenerated per VM — read it off the host, never assume |
| Cloud DNS zone | `baseproof-net` |

Static IP, firewall (`wg-hub` tag), A record and TXT record are in place and
survive VM replacement. Everything on the host itself — WireGuard, Caddy,
`hubd` — is rebuilt from scratch by the playbook on each new VM.

A 502 from `/healthz` means Caddy is up but `hubd` is not listening; check
`journalctl -u hubd`. A connection refused means Caddy is down or DNS is
pointing elsewhere.

## Layout

Each side owns its own provisioning; `infra/` is cloud resources only.

```
hub/                 everything that runs on the GCP VM
  hubd.go            enrollment, IP allocation, live peer programming
  ansible/
    playbook.yml     all hub configuration, declarative and idempotent
  startup.sh         GCP startup metadata; fetches a key, runs ansible-pull
client/              everything that runs on a Mac
  wg-egress.sh       installer + reconnect supervisor
  hubfind.json       agent config, filled in for this deployment
  hubfind/
    hubfind.go       hub discovery and failover agent
    hubfind_test.go  parsing, ranking, circuit breaker, live DNS
infra/               cloud resources, not host configuration
  gcp-hub-setup.sh   IP, firewall, VM, DNS
  gcp-publish-endpoint.sh   re-announce the hub IP from a rebuilt VM
  gcp-egress-check.sh       why the hub cannot reach the internet
```

The Caddyfile, systemd unit and `wg0.conf` are not checked in — the playbook
is their single source. Keeping copies alongside it only lets them drift.

## Provisioning

Configuration is an Ansible playbook (`hub/ansible/playbook.yml`), not a script.
It is idempotent: re-running after a code change rebuilds and restarts, and
changes nothing else.

Two ways to run it.

**Zero-touch at VM creation.** `hub/startup.sh` is injected as startup
metadata; it fetches a read-only deploy key from Secret Manager and hands off
to `ansible-pull`. No configuration in it — just an egress preflight, apt with
retries, and the handoff.

One-time setup:

```bash
gcloud services enable secretmanager.googleapis.com

# tr -d '\n' matters: gh emits a trailing newline that corrupts the token
gh auth refresh -h github.com -s repo -s read:packages -s write:packages
gh auth token | tr -d '\n' | gcloud secrets create gh-token --data-file=-

PROJECT_NUM=$(gcloud projects describe legalai-460612 --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding gh-token \
  --member="serviceAccount:$PROJECT_NUM-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor
```

Rotate by adding a version, not replacing the secret:

```bash
gh auth token | tr -d '\n' | gcloud secrets versions add gh-token --data-file=-
```

Then the VM provisions itself:

```bash
gcloud compute instances create wg-hub \
  --zone=us-central1-a --machine-type=e2-small \
  --image-family=debian-12 --image-project=debian-cloud \
  --address=hub-ip --can-ip-forward --tags=wg-hub \
  --scopes=cloud-platform \
  --metadata-from-file=startup-script=wg-egress/hub/startup.sh
```

`--scopes=cloud-platform` is required for Secret Manager access and cannot be
changed on a running instance.

Watch it converge:

```bash
gcloud compute ssh wg-hub --zone=us-central1-a --tunnel-through-iap \
  --command 'sudo journalctl -u google-startup-scripts -f'
```

**Against an existing VM**, from your Mac:

```bash
make hub-up
```

### A new VM means a new hub key

The playbook generates the WireGuard private key once and never regenerates
it, so re-running is safe. But a *fresh* VM has no key to preserve and will
mint a new one — which invalidates the `pk=` in your published TXT record.
After creating a replacement hub:

```bash
HUB_PK=$(gcloud compute ssh wg-hub --zone=us-central1-a --tunnel-through-iap \
  --command 'sudo wg show wg0 public-key' | tr -d '\r' | tail -n1)
gcloud dns record-sets update _hub.baseproof.net. --zone=baseproof-net \
  --type=TXT --ttl=60 --rrdatas="\"v=1 ep=34.72.191.179:51820 pri=10 pk=$HUB_PK\""
```

Enrolled nodes will not reconnect until that record matches, because their
configured peer key no longer exists on the hub.

## Finish the deployment

```bash
make deploy-hub
curl -s https://hub.baseproof.net/healthz
```

Expect `{"ok":true,"hub_pubkey":"<the key wg0 holds>"}`. It matching your
TXT record is what confirms the control plane and DNS agree about which hub
this is.

## Add a Mac

```bash
# on the hub
gcloud compute ssh wg-hub --zone=us-central1-a --tunnel-through-iap \
  --command 'sudo hubd join-token -node macmini-garage -class egress -ttl 1h'

# on the Mac
sudo ./wg-egress.sh enroll https://hub.baseproof.net <token>
sudo ./wg-egress.sh status
```

Enrollment is idempotent per node name: a wiped Mac re-enrolling as
`macmini-garage` is re-keyed in place and keeps its tunnel address.

## Add an RPA consumer

```bash
sudo hubd join-token -node rpa-vm-1 -class consumer -ttl 1h
```

Then in the RPA, ask which egress nodes are actually alive and use one:

```python
peers = requests.get("http://10.88.0.1:8080/v1/peers?class=egress&healthy=true",
                     headers={"authorization": f"Bearer {TOKEN}"}).json()["peers"]
proxies = {"http": peers[0]["proxy_url"], "https": peers[0]["proxy_url"]}
```

Health is measured from kernel handshake timestamps, not self-reported, so a
Mac that lost power drops out of the healthy set within three minutes.

## Operations

```bash
hubd enroll-key                  # show or create the single enrollment key
hubd enroll-key -rotate          # void every outstanding join token
hubd join-token -node N -class egress -ttl 1h
hubd revoke -client <id>         # drops the peer from the kernel immediately

sudo ./wg-egress.sh rotate       # new keypair, hub swaps it with no downtime
sudo ./wg-egress.sh status
sudo ./wg-egress.sh down
```

Rotation adds the new key before removing the old one, so in-flight sessions
survive. Revocation removes the peer from the device, so the next packet from
that node is dropped — unlike SSH key removal, which leaves live sessions up.

## Adding a second hub

```bash
gcloud dns record-sets update _hub.baseproof.net. --zone=baseproof-net \
  --type=TXT --ttl=60 \
  --rrdatas="\"v=1 ep=$IP_A:51820 pri=10 pk=$PK_A\",\"v=1 ep=$IP_B:51820 pri=20 pk=$PK_B\""
```

`hubfind` collects every TXT record. Priority breaks ties only among endpoints
it has not tried; once something has actually handshaken, measured history
outranks published intent.

## When the hub never comes up

The console shows a clean boot, DNS resolves, the metadata server answers — and
every package fetch times out:

```
E: Failed to fetch https://deb.debian.org/... connection timed out
Script "startup-script" failed with error: exit status 100
```

That is a VM with no path off the VPC. Nothing downstream ran: no WireGuard, no
Caddy, no `hubd`. The host is bare Debian that happens to be named `wg-hub`.

```bash
./infra/gcp-egress-check.sh          # from your Mac
./infra/gcp-egress-check.sh --probe  # and from inside the guest
```

It walks the whole path — external IP, Cloud NAT, Private Google Access,
default route, effective firewall, `canIpForward` — and names what it finds.
Two causes produce this exact pattern:

**No external IP, and no Cloud NAT.** The metadata server is link-local, so it
answers whether or not the VM has a way out; everything addressed to a public
IP is dropped at the VPC edge and the sender just waits. The giveaway is that
even Google's own APIs time out —
`dial tcp 172.217.119.4:443: i/o timeout` from the guest agent. The hub needs a
public address regardless, since peers dial UDP 51820 on it:

```bash
gcloud compute instances add-access-config wg-hub --zone=us-central1-a \
  --address=$(gcloud compute addresses describe hub-ip \
                --region=us-central1 --format='value(address)')
```

**An egress deny in a firewall policy.** `gcloud compute firewall-rules list`
will not show it. That command lists VPC firewall rules only: not the implied
allow-egress rule, and not the hierarchical policies on the org or folder or
the network policies attached to the VPC. A clean-looking listing is not
evidence of anything. The command that sees all of them is:

```bash
gcloud compute instances get-effective-firewalls wg-hub --zone=us-central1-a
```

Once the network is fixed, re-run provisioning in place — no need to recreate
the VM:

```bash
gcloud compute ssh wg-hub --zone=us-central1-a --tunnel-through-iap \
  --command 'sudo google_metadata_script_runner startup'
```

The startup script now refuses to continue when it cannot get out, and leaves
its reason in `/var/log/wg-egress-bootstrap.failed` instead of failing halfway
through apt. Retries cover a mirror that hiccups during boot; they do not cover
a VPC with no way out, and they are not meant to.

IPv6 lines like `connect (101: Network is unreachable)` are noise. The VM has a
link-local IPv6 address and no route off it, which is normal; apt now forces
IPv4 so those forty seconds of timeouts stop drowning the real error.

## Things worth knowing

**The enrollment key is a root credential.** One 32-byte key at
`/etc/hubd/enroll.key` on the hub; join tokens are HMACs derived from it and
nothing about them is stored. Anyone holding the key can mint a token for any
node name and class. It belongs only on the hub, mode 0600, backed up
separately from the database. The cost of statelessness is that individual
tokens cannot be revoked — only `-rotate`, which voids all of them.

**The serial console is not private.** `startup.sh` runs under `set -x`, and
until this was fixed the trace echoed the `gh-token` value on the way to
`/root/.git-credentials` — first as the assignment, then again on every
expansion. Anyone with `compute.instances.getSerialPortOutput` could read it,
and Cloud Logging kept it. Tracing is now off before the token exists and back
on after it is written. **If any hub VM ever got past `apt` on the old script,
rotate the secret:**

```bash
gh auth token | tr -d '\n' | gcloud secrets versions add gh-token --data-file=-
```

Nothing else in the script handles a credential, so nothing else needs the
treatment — but check the trace before adding something that does.

**DNS is not a trust boundary.** A poisoned TXT record points at an endpoint
that fails the WireGuard handshake; the agent marks it failed and moves on.
Authentication comes entirely from the peer's configured public key, which is
why the `pk=` field is advisory metadata rather than something clients verify.

**DoH can be blocked.** Running over 443 hides query content, not the
connection. eero Secure, TP-Link HomeShield, and Nest family filters all block
third-party DoH by IP or SNI, because they need to see DNS to enforce their own
filtering. The agent therefore falls through four tiers: Cloudflare, Google,
AdGuard, the router's own resolver, then the last address that actually
produced a handshake.

**`--can-ip-forward` cannot be changed after instance creation.** Without it
GCP silently drops packets not destined for the VM itself, so peer-to-peer
traffic through the hub vanishes with no error anywhere.

## Known limitations

- The hub is a single point of failure and a bandwidth choke; all RPA traffic
  crosses it twice. Regional hubs are the shape that scales, at the cost of
  per-hub peer state.
- `hubfind` tries candidates serially with an 8-second budget each, so three
  dead hubs ahead of a live one costs ~24 seconds. Racing them would need a
  scratch interface, since WireGuard allows one endpoint per peer.
- `wg-egress.sh` has been exercised against simulated network transitions, not
  against real router reboots and sleep cycles. Watch
  `/var/log/wg-egress/daemon.log` on the first Mac before rolling out further.
