#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ENV_PATH="${RUNTIME_ENV_PATH:-/etc/attendee/runtime.env}"
BOT_RUNTIME_IMAGE="${BOT_RUNTIME_IMAGE:-attendee-bot-runner:latest}"
BOT_RUNTIME_CONTAINER_NAME_PREFIX="${BOT_RUNTIME_CONTAINER_NAME_PREFIX:-attendee-bot}"
ATTENDEE_CONTAINER_WORKDIR="${ATTENDEE_CONTAINER_WORKDIR:-/attendee}"
METADATA_URL="${DROPLET_METADATA_ID_URL:-http://169.254.169.254/metadata/v1/id}"

if [[ -f "$RUNTIME_ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RUNTIME_ENV_PATH"
  set +a
fi

if [[ -z "${BOT_ID:-}" ]]; then
  echo "BOT_ID is required" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

CONTAINER_NAME="${BOT_RUNTIME_CONTAINER_NAME_PREFIX}-${BOT_ID}"

set +e
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run \
  --rm \
  --name "$CONTAINER_NAME" \
  --network host \
  --security-opt seccomp=unconfined \
  --shm-size=1g \
  -v "$RUNTIME_ENV_PATH:/run/attendee/runtime.env:ro" \
  "$BOT_RUNTIME_IMAGE" \
  bash -lc "set -a; source /run/attendee/runtime.env; set +a; cd \"$ATTENDEE_CONTAINER_WORKDIR\"; python manage.py run_bot --botid \"$BOT_ID\""
EXIT_CODE=$?
set -e

DROPLET_ID=""
if command -v curl >/dev/null 2>&1; then
  DROPLET_ID="$(curl -fsS --max-time 2 "$METADATA_URL" || true)"
fi

if [[ -n "${LEASE_CALLBACK_URL:-}" && -n "${LEASE_SHUTDOWN_TOKEN:-}" ]]; then
  CALLBACK_PAYLOAD="$(printf '{"bot_id":"%s","droplet_id":"%s","exit_code":%s,"reason":"process_exit"}' "${BOT_ID}" "${DROPLET_ID}" "${EXIT_CODE}")"
  curl -fsS \
    -X POST \
    -H "Authorization: Bearer ${LEASE_SHUTDOWN_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$CALLBACK_PAYLOAD" \
    "$LEASE_CALLBACK_URL" || true
fi

exit "$EXIT_CODE"
