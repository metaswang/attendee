#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-scripts/digitalocean/myvps.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${DROPLET_API_KEY:?DROPLET_API_KEY is required}"
: "${DO_TEMPLATE_DROPLET_NAME:?DO_TEMPLATE_DROPLET_NAME is required}"
: "${DO_TEMPLATE_BASE_IMAGE:?DO_TEMPLATE_BASE_IMAGE is required}"
: "${DO_TEMPLATE_SIZE_SLUG:?DO_TEMPLATE_SIZE_SLUG is required}"
: "${DO_BOT_REGION:?DO_BOT_REGION is required}"
: "${DO_BOT_SSH_KEY_IDS:?DO_BOT_SSH_KEY_IDS is required}"
: "${DO_TEMPLATE_TAGS:?DO_TEMPLATE_TAGS is required}"

if ! command -v doctl >/dev/null 2>&1; then
  echo "doctl is required" >&2
  exit 1
fi

export DIGITALOCEAN_ACCESS_TOKEN="$DROPLET_API_KEY"

CREATE_ARGS=(
  compute droplet create "$DO_TEMPLATE_DROPLET_NAME"
  --image "$DO_TEMPLATE_BASE_IMAGE"
  --size "$DO_TEMPLATE_SIZE_SLUG"
  --region "$DO_BOT_REGION"
  --ssh-keys "$DO_BOT_SSH_KEY_IDS"
  --tag-names "$DO_TEMPLATE_TAGS"
  --wait
  --format ID,Name,PublicIPv4
  --no-header
)

echo "Creating template droplet ${DO_TEMPLATE_DROPLET_NAME}..."
RESULT="$(doctl "${CREATE_ARGS[@]}")"
echo "$RESULT"

TEMPLATE_IP="$(awk '{print $3}' <<<"$RESULT")"
if [[ -n "$TEMPLATE_IP" ]]; then
  echo
  echo "SSH into the template droplet and run:"
  echo "  ssh root@${TEMPLATE_IP}"
  echo "  ATTENDEE_REPO_URL='${ATTENDEE_REPO_URL:-}' ATTENDEE_GIT_REF='${ATTENDEE_GIT_REF:-main}' BOT_RUNTIME_IMAGE='${BOT_RUNTIME_IMAGE:-attendee-bot-runner:latest}' bash -s < scripts/digitalocean/prepare-template-droplet.sh"
fi
