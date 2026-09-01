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

apt-get update -qq
apt-get install -y -qq git jq curl ansible

# The Cloud CLI is preinstalled on GCE Debian images and authenticates as the
# VM's own service account, so no credential file is needed to read the secret.
TOKEN="$(gcloud secrets versions access latest --secret=gh-token)"
[ -n "$TOKEN" ] || { echo "gh-token secret is empty or unreadable" >&2; exit 1; }

# x-access-token is the required username for GitHub tokens on both git and
# GHCR; using an account name here fails with a misleading 403.
git config --system credential.helper store
umask 077
printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > /root/.git-credentials

ansible-pull \
  --url https://github.com/baseproof/utils.git \
  --directory /opt/utils \
  --inventory localhost, \
  --checkout main \
  wg-egress/hub/ansible/playbook.yml
