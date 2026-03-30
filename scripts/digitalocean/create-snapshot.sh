#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-scripts/digitalocean/myvps.env}"
TARGET="${2:-}"

if [[ -z "$TARGET" ]]; then
  echo "usage: $0 <env-file> <droplet-id-or-name>" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${DROPLET_API_KEY:?DROPLET_API_KEY is required}"

if ! command -v doctl >/dev/null 2>&1; then
  echo "doctl is required" >&2
  exit 1
fi

export DIGITALOCEAN_ACCESS_TOKEN="$DROPLET_API_KEY"

resolve_droplet_id() {
  local target="$1"
  if [[ "$target" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$target"
    return
  fi
  doctl compute droplet list --format ID,Name --no-header | awk -v name="$target" '$2 == name {print $1; exit}'
}

DROPLET_ID="$(resolve_droplet_id "$TARGET")"
if [[ -z "$DROPLET_ID" ]]; then
  echo "Could not resolve droplet: $TARGET" >&2
  exit 1
fi

SNAPSHOT_NAME="attendee-bot-snapshot-$(date -u +%Y%m%d%H%M%S)"

echo "Shutting down droplet ${DROPLET_ID}..."
doctl compute droplet-action shutdown "$DROPLET_ID" --wait || true
doctl compute droplet-action power-off "$DROPLET_ID" --wait || true

echo "Creating snapshot ${SNAPSHOT_NAME}..."
doctl compute droplet-action snapshot "$DROPLET_ID" --snapshot-name "$SNAPSHOT_NAME" --wait

SNAPSHOT_ID="$(doctl compute snapshot list --resource droplet --format ID,Name --no-header | awk -v name="$SNAPSHOT_NAME" '$2 == name {print $1; exit}')"

echo
echo "Snapshot created:"
echo "  name: ${SNAPSHOT_NAME}"
echo "  id:   ${SNAPSHOT_ID}"
echo
echo "Set this on myvps:"
echo "  DO_BOT_SNAPSHOT_ID=${SNAPSHOT_ID}"
