#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ENV_PATH="${RUNTIME_ENV_PATH:-/etc/attendee/runtime.env}"
BOT_RUNTIME_IMAGE="${BOT_RUNTIME_IMAGE:-attendee-bot-runner:latest}"
BOT_RUNTIME_CONTAINER_NAME_PREFIX="${BOT_RUNTIME_CONTAINER_NAME_PREFIX:-attendee-bot}"
ATTENDEE_CONTAINER_WORKDIR="${ATTENDEE_CONTAINER_WORKDIR:-/attendee}"
METADATA_URL="${DROPLET_METADATA_ID_URL:-http://169.254.169.254/metadata/v1/id}"
PROVIDER_INSTANCE_ID_METADATA_URL="${PROVIDER_INSTANCE_ID_METADATA_URL:-$METADATA_URL}"
RUNNER_LOG_DIR="${RUNNER_LOG_DIR:-/var/log/attendee}"
RUNNER_LOG_PATH="${RUNNER_LOG_PATH:-${RUNNER_LOG_DIR}/runner.log}"
CONTAINER_LOG_PATH="${CONTAINER_LOG_PATH:-${RUNNER_LOG_DIR}/container.log}"
RUNNER_STATE_PATH="${RUNNER_STATE_PATH:-${RUNNER_LOG_DIR}/state.log}"
LOG_TAIL_LINES="${LOG_TAIL_LINES:-120}"

if [[ -f "$RUNTIME_ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RUNTIME_ENV_PATH"
  set +a
fi

mkdir -p "$RUNNER_LOG_DIR"
touch "$RUNNER_LOG_PATH" "$CONTAINER_LOG_PATH" "$RUNNER_STATE_PATH"
RUNNER_STARTED_AT="$(timestamp)"
RUNNER_STARTED_AT_MS="$(epoch_ms)"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

epoch_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

log_runner() {
  printf '%s %s\n' "$(timestamp)" "$*" | tee -a "$RUNNER_LOG_PATH" >&2
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

if [[ -z "${BOT_ID:-}" ]]; then
  log_runner "BOT_ID is required"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  log_runner "docker is required"
  exit 1
fi

CONTAINER_NAME="${BOT_RUNTIME_CONTAINER_NAME_PREFIX}-${BOT_ID}"
CONTAINER_ENV_PATH="${CONTAINER_ENV_PATH:-${RUNNER_LOG_DIR}/runtime.env}"
log_runner "Starting runner for bot ${BOT_ID} with image ${BOT_RUNTIME_IMAGE}"

cp "$RUNTIME_ENV_PATH" "$CONTAINER_ENV_PATH"
chmod 0644 "$CONTAINER_ENV_PATH"
printf '%s runner_started_at=%s runner_started_at_ms=%s\n' "$(timestamp)" "$RUNNER_STARTED_AT" "$RUNNER_STARTED_AT_MS" >> "$RUNNER_STATE_PATH"

set +e
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
CONTAINER_START_AT="$(timestamp)"
CONTAINER_START_AT_MS="$(epoch_ms)"
docker run \
  --rm \
  --name "$CONTAINER_NAME" \
  --network host \
  --user root \
  --security-opt seccomp=unconfined \
  --shm-size=1g \
  -v "$CONTAINER_ENV_PATH:/run/attendee/runtime.env:ro" \
  "$BOT_RUNTIME_IMAGE" \
  bash -lc "set -euo pipefail; set -a; source /run/attendee/runtime.env; set +a; cd \"$ATTENDEE_CONTAINER_WORKDIR\"; export DJANGO_SETTINGS_MODULE=\"\${DJANGO_SETTINGS_MODULE:-attendee.settings.production}\"; exec python manage.py run_bot --botid \"$BOT_ID\"" \
  2>&1 | tee -a "$CONTAINER_LOG_PATH"
EXIT_CODE=$?
set -e

CONTAINER_FINISHED_AT="$(timestamp)"
CONTAINER_FINISHED_AT_MS="$(epoch_ms)"
printf '%s container_start_at=%s container_start_at_ms=%s container_finished_at=%s container_finished_at_ms=%s exit_code=%s\n' "$(timestamp)" "$CONTAINER_START_AT" "$CONTAINER_START_AT_MS" "$CONTAINER_FINISHED_AT" "$CONTAINER_FINISHED_AT_MS" "$EXIT_CODE" >> "$RUNNER_STATE_PATH"
log_runner "Container finished with exit_code=${EXIT_CODE}"

PROVIDER_INSTANCE_ID=""
if command -v curl >/dev/null 2>&1; then
  PROVIDER_INSTANCE_ID="$(curl -fsS --max-time 2 "$PROVIDER_INSTANCE_ID_METADATA_URL" || true)"
fi

FINAL_STATE="succeeded"
if [[ "$EXIT_CODE" -ne 0 ]]; then
  FINAL_STATE="failed"
fi

LOG_TAIL="$(tail -n "$LOG_TAIL_LINES" "$CONTAINER_LOG_PATH" 2>/dev/null || true)"
if [[ -z "$LOG_TAIL" ]]; then
  LOG_TAIL="$(tail -n "$LOG_TAIL_LINES" "$RUNNER_LOG_PATH" 2>/dev/null || true)"
fi

if [[ -n "${LEASE_CALLBACK_URL:-}" && -n "${LEASE_SHUTDOWN_TOKEN:-}" ]]; then
  LOG_TAIL_JSON="$(printf '%s' "$LOG_TAIL" | json_escape)"
  CALLBACK_PAYLOAD="$(printf '{"bot_id":"%s","provider_instance_id":"%s","droplet_id":"%s","exit_code":%s,"final_state":"%s","reason":"process_exit","log_tail":%s,"runner_log_path":"%s","container_log_path":"%s","runner_started_at":"%s","runner_started_at_ms":%s,"container_start_at":"%s","container_start_at_ms":%s,"container_finished_at":"%s","container_finished_at_ms":%s,"bot_launch_requested_at":"%s"}' \
    "${BOT_ID}" \
    "${PROVIDER_INSTANCE_ID}" \
    "${PROVIDER_INSTANCE_ID}" \
    "${EXIT_CODE}" \
    "${FINAL_STATE}" \
    "${LOG_TAIL_JSON}" \
    "${RUNNER_LOG_PATH}" \
    "${CONTAINER_LOG_PATH}" \
    "${RUNNER_STARTED_AT}" \
    "${RUNNER_STARTED_AT_MS}" \
    "${CONTAINER_START_AT}" \
    "${CONTAINER_START_AT_MS}" \
    "${CONTAINER_FINISHED_AT}" \
    "${CONTAINER_FINISHED_AT_MS}" \
    "${BOT_LAUNCH_REQUESTED_AT:-}")"
  curl -fsS \
    -X POST \
    -H "Authorization: Bearer ${LEASE_SHUTDOWN_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$CALLBACK_PAYLOAD" \
    "$LEASE_CALLBACK_URL" || true
fi

exit "$EXIT_CODE"
