#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ENV_PATH="${RUNTIME_ENV_PATH:-/etc/attendee/runtime.env}"
BOT_RUNTIME_IMAGE="${BOT_RUNTIME_IMAGE:-attendee-bot-runner:latest}"
BOT_RUNTIME_CONTAINER_NAME_PREFIX="${BOT_RUNTIME_CONTAINER_NAME_PREFIX:-attendee-bot}"
ATTENDEE_REPO_DIR="${ATTENDEE_REPO_DIR:-/opt/attendee}"
ATTENDEE_CONTAINER_WORKDIR="${ATTENDEE_CONTAINER_WORKDIR:-/attendee}"
METADATA_URL="${DROPLET_METADATA_ID_URL:-http://169.254.169.254/metadata/v1/id}"
PROVIDER_INSTANCE_ID_METADATA_URL="${PROVIDER_INSTANCE_ID_METADATA_URL:-$METADATA_URL}"
RUNNER_LOG_DIR="${RUNNER_LOG_DIR:-/var/log/attendee}"
RUNNER_LOG_PATH="${RUNNER_LOG_PATH:-${RUNNER_LOG_DIR}/runner.log}"
CONTAINER_LOG_PATH="${CONTAINER_LOG_PATH:-${RUNNER_LOG_DIR}/container.log}"
RUNNER_STATE_PATH="${RUNNER_STATE_PATH:-${RUNNER_LOG_DIR}/state.log}"
LOG_TAIL_LINES="${LOG_TAIL_LINES:-120}"
BOT_MEMORY_LIMIT="${BOT_MEMORY_LIMIT:-${MEETBOT_BOT_MEMORY_LIMIT:-512m}}"
BOT_MEMORY_RESERVATION="${BOT_MEMORY_RESERVATION:-${MEETBOT_BOT_MEMORY_RESERVATION:-512m}}"
BOT_SHM_SIZE="${BOT_SHM_SIZE:-${MEETBOT_BOT_SHM_SIZE:-1g}}"
BOT_RUNTIME_SOURCE_ARCHIVE_URL="${BOT_RUNTIME_SOURCE_ARCHIVE_URL:-}"

if [[ -f "$RUNTIME_ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RUNTIME_ENV_PATH"
  set +a
fi

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

epoch_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

sync_attendee_source_archive() {
  local repo_dir
  local source_archive_url
  local temp_dir
  repo_dir="${ATTENDEE_REPO_DIR:-/opt/attendee}"
  source_archive_url="${BOT_RUNTIME_SOURCE_ARCHIVE_URL:-}"
  if [[ -z "$source_archive_url" ]]; then
    return 0
  fi
  if [[ -z "${LEASE_SHUTDOWN_TOKEN:-}" ]]; then
    log_runner "Skipping source archive sync because LEASE_SHUTDOWN_TOKEN is missing"
    return 1
  fi
  temp_dir="$(mktemp -d)"
  log_runner "Syncing source archive into $repo_dir from $source_archive_url"
  if ! curl -fsSL --retry 3 --connect-timeout 10 --max-time 180 -H "Authorization: Bearer ${LEASE_SHUTDOWN_TOKEN}" "$source_archive_url" | tar -xzf - -C "$temp_dir"; then
    rm -rf "$temp_dir"
    log_runner "Source archive sync failed"
    return 1
  fi
  rm -rf "$repo_dir"
  mkdir -p "$repo_dir"
  cp -a "$temp_dir/." "$repo_dir/"
  rm -rf "$temp_dir"
  log_runner "Source archive sync complete"
  return 0
}

sync_attendee_repo() {
  local git_bin
  local repo_dir
  local repo_url
  local git_ref
  git_bin="$(command -v git 2>/dev/null || true)"
  repo_dir="${ATTENDEE_REPO_DIR:-/opt/attendee}"
  repo_url="${ATTENDEE_REPO_URL:-}"
  git_ref="${ATTENDEE_GIT_REF:-main}"
  if [[ -z "$git_bin" || -z "$repo_url" ]]; then
    return 0
  fi
  if [[ -d "$repo_dir/.git" ]]; then
    timeout 120 "$git_bin" -C "$repo_dir" fetch --all --tags
    if "$git_bin" -C "$repo_dir" show-ref --verify --quiet "refs/remotes/origin/${git_ref}"; then
      timeout 120 "$git_bin" -C "$repo_dir" checkout -B "$git_ref" "origin/${git_ref}"
    fi
  else
    rm -rf "$repo_dir"
    timeout 120 "$git_bin" clone --depth 1 --branch "$git_ref" "$repo_url" "$repo_dir"
  fi
  return 0
}

mkdir -p "$RUNNER_LOG_DIR"
touch "$RUNNER_LOG_PATH" "$CONTAINER_LOG_PATH" "$RUNNER_STATE_PATH"
RUNNER_STARTED_AT="$(timestamp)"
RUNNER_STARTED_AT_MS="$(epoch_ms)"

log_runner() {
  printf '%s %s\n' "$(timestamp)" "$*" | tee -a "$RUNNER_LOG_PATH" >&2
}

if [[ -z "${BOT_ID:-}" && -z "${LEASE_ID:-}" ]]; then
  log_runner "BOT_ID or LEASE_ID is required"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  log_runner "docker is required"
  exit 1
fi

BOT_IDENTIFIER="${BOT_ID:-lease-${LEASE_ID:-unknown}}"
CONTAINER_NAME="${BOT_RUNTIME_CONTAINER_NAME_PREFIX}-${BOT_IDENTIFIER}"
CONTAINER_ENV_PATH="${CONTAINER_ENV_PATH:-${RUNNER_LOG_DIR}/runtime.env}"
log_runner "Starting runner for bot ${BOT_ID} with image ${BOT_RUNTIME_IMAGE}"

cp "$RUNTIME_ENV_PATH" "$CONTAINER_ENV_PATH"
chmod 0644 "$CONTAINER_ENV_PATH"
printf '%s runner_started_at=%s runner_started_at_ms=%s\n' "$(timestamp)" "$RUNNER_STARTED_AT" "$RUNNER_STARTED_AT_MS" >> "$RUNNER_STATE_PATH"

if ! sync_attendee_source_archive; then
  sync_attendee_repo || true
fi

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
  --memory="$BOT_MEMORY_LIMIT" \
  --memory-reservation="$BOT_MEMORY_RESERVATION" \
  --shm-size="$BOT_SHM_SIZE" \
  -v "$ATTENDEE_REPO_DIR:$ATTENDEE_CONTAINER_WORKDIR" \
  -v "$CONTAINER_ENV_PATH:/run/attendee/runtime.env:ro" \
  "$BOT_RUNTIME_IMAGE" \
    bash -lc "set -euo pipefail; set -a; source /run/attendee/runtime.env; set +a; cd \"$ATTENDEE_CONTAINER_WORKDIR\"; export DJANGO_SETTINGS_MODULE=\"\${DJANGO_SETTINGS_MODULE:-attendee.settings.bot_runtime}\"; exec python manage.py run_bot --skip-checks \${LEASE_ID:+--lease-id \"\$LEASE_ID\"} \${BOT_ID:+--botid \"\$BOT_ID\"}" \
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
