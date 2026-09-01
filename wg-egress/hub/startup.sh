#!/bin/bash
# wg-egress/hub/startup.sh
#
# Injected as GCP startup-script metadata. Fetches a GitHub token from Secret
# Manager and hands control to Ansible. All configuration lives in
# wg-egress/hub/ansible/playbook.yml.
#
#   gcloud compute instances create wg-hub ... \
#     --metadata-from-file=startup-script=wg-egress/hub/startup.sh
#
# Requires: --scopes=cloud-platform, and a Secret Manager secret named
# gh-token holding a GitHub token with repo + read:packages.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

FAILED=/var/log/wg-egress-bootstrap.failed
rm -f "$FAILED"

# Every fetch below is IPv4. This VM has a link-local IPv6 address and no route
# off it, so each unforced apt attempt burns ~40s failing over four AAAA
# records before it tries an address it can actually reach.
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99-force-ipv4

# ---------------------------------------------------------------- preflight

# A hub with no egress cannot install anything, and every later failure is a
# confusing restatement of this one. The metadata server is link-local and
# answers either way, so the console looks healthy while every package fetch
# times out. Say it once, in the words that name the actual cause.
no_egress() {
  cat > "$FAILED" <<'MSG'
wg-egress bootstrap aborted: this VM has no route to the internet.

DNS resolves (the metadata server is link-local and always answers) but every
TCP connection to a public address times out. On GCE that is one of:

  * no external IP on the instance, and no Cloud NAT for its subnet
  * an EGRESS deny in a hierarchical or network firewall policy
    -- `gcloud compute firewall-rules list` does NOT show those

Diagnose with wg-egress/infra/gcp-egress-check.sh, fix the network, then
re-run the startup script:

  sudo google_metadata_script_runner startup
MSG
  { echo "=== wg-egress bootstrap aborted ==="; cat "$FAILED"; } >&2
  exit 1
}

# bash's own /dev/tcp is the fallback because it needs nothing installed, and a
# bare TCP connect is exactly the thing that is failing.
reaches_internet() {
  if command -v curl >/dev/null 2>&1; then
    curl -4 -sS -m 10 -o /dev/null https://deb.debian.org/
  else
    timeout 10 bash -c '</dev/tcp/deb.debian.org/443'
  fi
}

# Resolution is checked separately so the message can tell a DNS failure apart
# from a black hole; apt reports both as the same failed fetch.
getent hosts deb.debian.org >/dev/null || no_egress
for attempt in 1 2 3 4 5 6; do
  if reaches_internet; then break; fi
  [ "$attempt" -lt 6 ] || no_egress
  sleep $(( attempt * 5 ))
done

# ---------------------------------------------------------------- packages

# A mirror that hiccups during boot should cost a retry, not the whole hub:
# without this, one failed fetch leaves an unprovisioned VM that still looks
# up, and nothing downstream ever runs.
apt_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if apt-get "$@"; then return 0; fi
    sleep $(( attempt * 10 ))
  done
  return 1
}

# Reaching this line means the network is fine, so a failure here is the
# mirror or the archive, not the VPC. Say which, so the next reader does not
# re-litigate the question above.
apt_failed() { echo "apt failed after retries: mirror or archive, not the VPC" >&2; exit 1; }
apt_retry update -qq || apt_failed
apt_retry install -y -qq git jq curl ansible || apt_failed

# ---------------------------------------------------------------- handoff

# Tracing goes off before the token exists, not before it is used: `set -x`
# echoes the assignment itself, and every later expansion of it. Startup-script
# traces land in the serial console, which anyone holding
# compute.instances.getSerialPortOutput can read, and which Cloud Logging keeps.
set +x

# The Cloud CLI is preinstalled on GCE Debian images and authenticates as the
# VM's own service account, so no credential file is needed to read the secret.
TOKEN="$(gcloud secrets versions access latest --secret=gh-token)"
[ -n "$TOKEN" ] || { echo "gh-token secret is empty or unreadable" >&2; exit 1; }

# x-access-token is the required username for GitHub tokens on both git and
# GHCR; using an account name here fails with a misleading 403.
git config --system credential.helper store
umask 077
printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > /root/.git-credentials
unset TOKEN
set -x

ansible-pull \
  --url https://github.com/baseproof/utils.git \
  --directory /opt/utils \
  --inventory localhost, \
  --checkout main \
  wg-egress/hub/ansible/playbook.yml
