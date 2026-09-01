#!/bin/bash
# wg-egress/hub/startup.sh
#
# Injected as GCP startup-script metadata. The repository is public, so there
# is no credential to fetch — this waits for the network and hands control to
# Ansible. All configuration lives in wg-egress/hub/ansible/playbook.yml.
#
#   gcloud compute instances create wg-hub ... \
#     --metadata-from-file=startup-script=wg-egress/hub/startup.sh
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

ansible-pull \
  --url https://github.com/baseproof/utils.git \
  --directory /opt/utils \
  --inventory localhost, \
  --checkout main \
  wg-egress/hub/ansible/playbook.yml