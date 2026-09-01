#!/usr/bin/env bash
#
# gcp-publish-endpoint.sh — runs ON the GCP VM.
#
# Reads the VM's current external IP from the metadata server and publishes it
# as the A record the Mac mini resolves over DoH. Run it from the instance
# startup-script so a rebuilt or restarted VM re-announces itself.
#
#   PROVIDER=clouddns DNS_NAME=rpa-egress.example.com DNS_ZONE=my-zone ./gcp-publish-endpoint.sh
#   PROVIDER=cloudflare DNS_NAME=rpa-egress.example.com CF_ZONE_ID=... CF_API_TOKEN=... ./gcp-publish-endpoint.sh
#
set -euo pipefail

PROVIDER="${PROVIDER:-clouddns}"
DNS_NAME="${DNS_NAME:?set DNS_NAME, e.g. rpa-egress.example.com}"
TTL="${TTL:-60}"

MD="http://metadata.google.internal/computeMetadata/v1"
IP="$(curl -fsS -H 'Metadata-Flavor: Google' \
  "$MD/instance/network-interfaces/0/access-configs/0/external-ip")"

[[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "no external IP found" >&2; exit 1; }
echo "current external IP: $IP"

case "$PROVIDER" in
  clouddns)
    : "${DNS_ZONE:?set DNS_ZONE (the Cloud DNS managed zone name)}"
    FQDN="${DNS_NAME%.}."
    CURRENT="$(gcloud dns record-sets list --zone="$DNS_ZONE" --name="$FQDN" \
                 --type=A --format='value(rrdatas[0])' 2>/dev/null || true)"
    if [[ "$CURRENT" == "$IP" ]]; then
      echo "already current, nothing to do"
    elif [[ -n "$CURRENT" ]]; then
      gcloud dns record-sets update "$FQDN" --zone="$DNS_ZONE" \
        --type=A --ttl="$TTL" --rrdatas="$IP"
      echo "updated $CURRENT -> $IP"
    else
      gcloud dns record-sets create "$FQDN" --zone="$DNS_ZONE" \
        --type=A --ttl="$TTL" --rrdatas="$IP"
      echo "created $FQDN -> $IP"
    fi
    ;;

  cloudflare)
    : "${CF_ZONE_ID:?set CF_ZONE_ID}" "${CF_API_TOKEN:?set CF_API_TOKEN}"
    API="https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records"
    REC_ID="$(curl -fsS -H "Authorization: Bearer $CF_API_TOKEN" \
                "$API?type=A&name=$DNS_NAME" | jq -r '.result[0].id // empty')"
    BODY="$(jq -nc --arg n "$DNS_NAME" --arg c "$IP" --argjson t "$TTL" \
              '{type:"A",name:$n,content:$c,ttl:$t,proxied:false}')"
    if [[ -n "$REC_ID" ]]; then
      curl -fsS -X PUT "$API/$REC_ID" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H 'content-type: application/json' --data "$BODY" | jq -r '.success'
    else
      curl -fsS -X POST "$API" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H 'content-type: application/json' --data "$BODY" | jq -r '.success'
    fi
    echo "published $DNS_NAME -> $IP"
    ;;

  *) echo "unknown PROVIDER: $PROVIDER" >&2; exit 1 ;;
esac
