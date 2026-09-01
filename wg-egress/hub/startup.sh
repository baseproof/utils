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
