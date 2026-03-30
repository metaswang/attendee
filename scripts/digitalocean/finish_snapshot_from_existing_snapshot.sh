#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/myvps.env" >&2
  exit 1
fi

ENV_FILE="$1"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

source "$ENV_FILE"
export DIGITALOCEAN_ACCESS_TOKEN="$DROPLET_API_KEY"

TMP_NAME="attendee-bot-template-v4"
SNAPSHOT_NAME="attendee-bot-snapshot-$(date +%Y%m%d%H%M%S)"

cleanup() {
  env DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_ACCESS_TOKEN" \
    doctl compute droplet delete "$TMP_NAME" --force >/dev/null 2>&1 || true
}

trap cleanup EXIT

doctl compute droplet create "$TMP_NAME" \
  --size "$DO_TEMPLATE_SIZE_SLUG" \
  --image "$DO_BOT_SNAPSHOT_ID" \
  --region "$DO_BOT_REGION" \
  --ssh-keys "$DO_BOT_SSH_KEY_IDS" \
  --tag-names "${DO_TEMPLATE_TAGS:-attendee-template,env-prod}" \
  --wait \
  --format ID,Name,PublicIPv4,Status \
  --no-header

DROPLET_ID="$(doctl compute droplet list --format ID,Name --no-header | awk '$2=="'"$TMP_NAME"'" {print $1; exit}')"
DROPLET_IP="$(doctl compute droplet list --format Name,PublicIPv4 --no-header | awk '$1=="'"$TMP_NAME"'" {print $2; exit}')"

if [[ -z "$DROPLET_ID" || -z "$DROPLET_IP" ]]; then
  echo "failed to resolve temp droplet id/ip for $TMP_NAME" >&2
  exit 1
fi

for _ in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "root@$DROPLET_IP" "true" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

scp -o StrictHostKeyChecking=no scripts/digitalocean/attendee-bot-runner.sh "root@$DROPLET_IP:/usr/local/bin/attendee-bot-runner"
scp -o StrictHostKeyChecking=no scripts/digitalocean/attendee-bot-runner.service "root@$DROPLET_IP:/etc/systemd/system/attendee-bot-runner.service"

ssh -o StrictHostKeyChecking=no "root@$DROPLET_IP" '
  chmod 0755 /usr/local/bin/attendee-bot-runner &&
  chmod 0644 /etc/systemd/system/attendee-bot-runner.service &&
  systemctl daemon-reload &&
  systemctl disable attendee-bot-runner.service >/dev/null 2>&1 || true &&
  cloud-init clean --logs || true &&
  truncate -s 0 /etc/machine-id || true &&
  rm -f /var/lib/dbus/machine-id || true &&
  sync &&
  poweroff
'

for _ in $(seq 1 30); do
  STATUS="$(doctl compute droplet get "$DROPLET_ID" --format Status --no-header || true)"
  if [[ "$STATUS" == "off" ]]; then
    break
  fi
  sleep 5
done

doctl compute droplet-action snapshot "$DROPLET_ID" --snapshot-name "$SNAPSHOT_NAME" --wait
SNAPSHOT_ID="$(doctl compute snapshot list --resource droplet --format ID,Name --no-header | awk '$2=="'"$SNAPSHOT_NAME"'" {print $1; exit}')"

if [[ -z "$SNAPSHOT_ID" ]]; then
  echo "snapshot id not found for $SNAPSHOT_NAME" >&2
  exit 1
fi

printf 'SNAPSHOT_ID=%s\nSNAPSHOT_NAME=%s\nDROPLET_ID=%s\n' "$SNAPSHOT_ID" "$SNAPSHOT_NAME" "$DROPLET_ID"
