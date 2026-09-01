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
# gh-deploy-key holding the private half of a read-only deploy key.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Egress is not always ready when the startup script runs — a freshly attached
# external IP can take a couple of minutes for NAT to be programmed. Without
# this the first apt call fails and the whole provision dies at boot.
for i in $(seq 1 60); do
  if curl -fsS -m 5 -o /dev/null https://deb.debian.org/ 2>/dev/null; then
    echo "egress ready after ${i} attempts"
    break
  fi
  [ "$i" -eq 60 ] && { echo "no outbound internet after 5 minutes" >&2; exit 1; }
  sleep 5
done

# This VPC has no IPv6 route, so every AAAA attempt burns a connect timeout
# before apt falls back. Skip them.
cat > /etc/apt/apt.conf.d/99-startup <<'APT'
Acquire::ForceIPv4 "true";
Acquire::Retries "5";
Acquire::http::Timeout "20";
APT

apt-get update -qq
apt-get install -y -qq git jq curl ansible

# The Cloud CLI is preinstalled on GCE Debian images and authenticates as the
# VM's own service account, so no credential file is needed to read the secret.
# xtrace is on for the rest of this script, but it must not see the key —
# startup-script output goes to the serial console and Cloud Logging, both of
# which are readable by anyone with project viewer.
set +x
install -d -m 700 /root/.ssh
gcloud secrets versions access latest --secret=gh-deploy-key > /root/.ssh/gh_deploy
chmod 600 /root/.ssh/gh_deploy
if ! [ -s /root/.ssh/gh_deploy ]; then
  set -x
  echo "gh-deploy-key secret is empty or unreadable" >&2
  exit 1
fi
set -x

# Pin GitHub's host keys rather than trusting whatever answers first. The meta
# API is rate limited, so fall back to ssh-keyscan and refuse to run unpinned.
curl -s https://api.github.com/meta \
  | jq -r '.ssh_keys[]? | "github.com " + .' > /root/.ssh/known_hosts || true
if ! [ -s /root/.ssh/known_hosts ]; then
  ssh-keyscan -t rsa,ecdsa,ed25519 github.com > /root/.ssh/known_hosts 2>/dev/null || true
fi
[ -s /root/.ssh/known_hosts ] || { echo "could not obtain github host keys" >&2; exit 1; }

export GIT_SSH_COMMAND="ssh -i /root/.ssh/gh_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=/root/.ssh/known_hosts"

ansible-pull \
  --url git@github.com:baseproof/utils.git \
  --directory /opt/utils \
  --inventory localhost, \
  --checkout main \
  wg-egress/hub/ansible/playbook.yml