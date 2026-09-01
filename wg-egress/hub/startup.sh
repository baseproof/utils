#!/bin/bash
# wg-egress/hub/startup.sh
#
# Injected as GCP startup-script metadata. Does nothing but obtain a read-only
# deploy key and hand control to Ansible. All actual configuration lives in
# wg-egress/hub/ansible/playbook.yml.
#
#   gcloud compute instances create wg-hub ... \
#     --metadata-from-file=startup-script=wg-egress/hub/startup.sh
#
# Requires: --scopes=cloud-platform, and a Secret Manager secret named
# gh-deploy-key holding the private half of a read-only GitHub deploy key.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq git jq curl ansible

MD="http://metadata.google.internal/computeMetadata/v1"
TOKEN=$(curl -sH 'Metadata-Flavor: Google' "$MD/instance/service-accounts/default/token" | jq -r .access_token)
PROJECT=$(curl -sH 'Metadata-Flavor: Google' "$MD/project/project-id")

install -d -m 700 /root/.ssh
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/gh-deploy-key/versions/latest:access" \
  | jq -r .payload.data | base64 -d > /root/.ssh/gh_deploy
chmod 600 /root/.ssh/gh_deploy

# Pin GitHub's host keys from their TLS-served metadata rather than trusting
# whatever answers on first connect. That API is rate limited, so fall back to
# ssh-keyscan and refuse to continue with an empty file — an unpinned clone
# would fail later with a far less obvious error.
curl -s https://api.github.com/meta \
  | jq -r '.ssh_keys[]? | "github.com " + .' > /root/.ssh/known_hosts || true
if [ ! -s /root/.ssh/known_hosts ]; then
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
