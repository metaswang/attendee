#!/usr/bin/env bash
set -euo pipefail

: "${ATTENDEE_REPO_URL:?ATTENDEE_REPO_URL is required}"
: "${BOT_RUNTIME_IMAGE:?BOT_RUNTIME_IMAGE is required}"

ATTENDEE_GIT_REF="${ATTENDEE_GIT_REF:-main}"
ATTENDEE_REPO_DIR="${ATTENDEE_REPO_DIR:-/opt/attendee}"
RUNNER_SCRIPT_SOURCE="${RUNNER_SCRIPT_SOURCE:-scripts/digitalocean/attendee-bot-runner.sh}"
RUNNER_SERVICE_SOURCE="${RUNNER_SERVICE_SOURCE:-scripts/digitalocean/attendee-bot-runner.service}"
BUILD_RUNTIME_IMAGE="${BUILD_RUNTIME_IMAGE:-true}"
PULL_RUNTIME_IMAGE="${PULL_RUNTIME_IMAGE:-true}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
BOT_RUNTIME_DOCKERFILE="${BOT_RUNTIME_DOCKERFILE:-Dockerfile.bot-runtime}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates cloud-init curl git jq redis-tools

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker

if [[ ! -d "$ATTENDEE_REPO_DIR/.git" ]]; then
  git clone "$ATTENDEE_REPO_URL" "$ATTENDEE_REPO_DIR"
fi

cd "$ATTENDEE_REPO_DIR"
git fetch --all --tags

if git show-ref --verify --quiet "refs/remotes/origin/${ATTENDEE_GIT_REF}"; then
  git checkout -B "${ATTENDEE_GIT_REF}" "origin/${ATTENDEE_GIT_REF}"
else
  git checkout "${ATTENDEE_GIT_REF}"
fi

if [[ "$BUILD_RUNTIME_IMAGE" == "true" ]]; then
  docker build --platform "$DOCKER_PLATFORM" -f "$BOT_RUNTIME_DOCKERFILE" -t "$BOT_RUNTIME_IMAGE" .
fi

if [[ "$PULL_RUNTIME_IMAGE" == "true" ]]; then
  docker pull "$BOT_RUNTIME_IMAGE"
fi

docker image inspect "$BOT_RUNTIME_IMAGE" >/dev/null

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "Required Python interpreter not found: ${PYTHON_BIN}" >&2
  exit 1
}

install -D -m 0755 "$RUNNER_SCRIPT_SOURCE" /usr/local/bin/attendee-bot-runner
install -D -m 0644 "$RUNNER_SERVICE_SOURCE" /etc/systemd/system/attendee-bot-runner.service
install -D -m 0755 scripts/runtime_agent.py /usr/local/bin/attendee-runtime-agent
install -D -m 0644 scripts/digitalocean/attendee-runtime-agent.service /etc/systemd/system/attendee-runtime-agent.service
mkdir -p /etc/attendee /var/log/attendee

systemctl daemon-reload
systemctl disable attendee-bot-runner.service >/dev/null 2>&1 || true
systemctl enable attendee-runtime-agent.service >/dev/null 2>&1 || true

cloud-init clean --logs
truncate -s 0 /etc/machine-id || true
rm -f /var/lib/dbus/machine-id || true

echo
echo "Golden image builder VM prepared."
echo "Next steps:"
echo "  1. Stop this VM."
echo "  2. Create a custom image from this disk."
echo "  3. Publish or update the image family backing GCP_BOT_SOURCE_IMAGE_FAMILY."
echo "  4. Point BOT_RUNTIME_IMAGE at the pre-pulled runtime image digest baked into this image."
