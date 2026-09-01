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
| Hub VM | `hub-ip`, zone `us-central1-a`, project `legalai-460612` |
| Hub address | `34.72.191.179` (reserved as `hub-ip`) |
| Hostname | `hub.baseproof.net` |
| Discovery record | `_hub.baseproof.net` TXT |
| Hub WireGuard key | `irayR4YCD4cQpFbvR2c0ZeU8CC9PgsOPYnJiTNQzJUo=` |
| Cloud DNS zone | `baseproof-net` |

Done: static IP, VM with `--can-ip-forward`, firewall (`wg-hub` tag),
WireGuard `wg0` at `10.88.0.1/16`, A record, TXT record, Caddy with a valid
certificate.

Not done: `hubd` is not deployed. `https://hub.baseproof.net/healthz` returns
502 until it is — that 502 is Caddy proxying to a port nothing listens on.

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
to `ansible-pull`. Twenty lines, none of them configuration.

One-time setup:

```bash
gcloud services enable secretmanager.googleapis.com
gcloud secrets create gh-deploy-key --data-file=./gh_deploy   # private half of a READ-ONLY deploy key
PROJECT_NUM=$(gcloud projects describe legalai-460612 --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding gh-deploy-key \
  --member="serviceAccount:$PROJECT_NUM-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor
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

Expect `{"ok":true,"hub_pubkey":"irayR4YCD4cQpFbvR2c0ZeU8CC9PgsOPYnJiTNQzJUo="}`.
The key matching the TXT record confirms the control plane is reading the same
device you published.

## Add a Mac

```bash
# on the hub
gcloud compute ssh hub-ip --zone=us-central1-a --tunnel-through-iap \
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

## Things worth knowing

**The enrollment key is a root credential.** One 32-byte key at
`/etc/hubd/enroll.key` on the hub; join tokens are HMACs derived from it and
nothing about them is stored. Anyone holding the key can mint a token for any
node name and class. It belongs only on the hub, mode 0600, backed up
separately from the database. The cost of statelessness is that individual
tokens cannot be revoked — only `-rotate`, which voids all of them.

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
