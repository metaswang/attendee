#!/usr/bin/env bash
set -euo pipefail

# Prepare a GCP builder VM into a voxella-attendee golden image source.
#
# Two acquisition modes for the bot runtime docker image are supported:
#
#   1. PULL_RUNTIME_IMAGE=true  (default)
#        Pull a pre-built image from a registry (e.g. Docker Hub). This is
#        the recommended flow; build is done on myvps so the golden image
#        is guaranteed byte-identical to what runs on the VPS fleet.
#
#   2. BUILD_RUNTIME_IMAGE=true
#        Legacy fallback: build the bot runtime image from the local
#        $ATTENDEE_REPO_DIR. Used only when a Hub push is not available.
#
# Exactly one of the two modes must be true.
#
# ATTENDEE_REPO_URL accepts either:
#   - a git remote URL (cloned into $ATTENDEE_REPO_DIR), or
#   - an absolute path to a directory on the builder VM (copied into
#     $ATTENDEE_REPO_DIR as-is; no git operations).
# The latter is preferred for PULL mode because only support scripts
# (runner.sh / runner.service / runtime_agent.py) need to be on-disk.
#
# Optional:
#   BOT_RUNTIME_IMAGE_ALIAS  extra local tag applied to the acquired image
#                            (default: attendee-bot-runner:latest). The
#                            systemd runner unit launches containers by
#                            this alias, so a stable local name decouples
#                            bot-runner.service from the remote registry
#                            path.
#   DOCKER_LOGOUT_AFTER      if "true" (default when PULL), `docker logout`
#                            every registry referenced by BOT_RUNTIME_IMAGE
#                            before exiting, to avoid baking credentials
#                            into the golden image snapshot.

: "${ATTENDEE_REPO_URL:?ATTENDEE_REPO_URL is required}"
: "${BOT_RUNTIME_IMAGE:?BOT_RUNTIME_IMAGE is required}"

ATTENDEE_GIT_REF="${ATTENDEE_GIT_REF:-main}"
ATTENDEE_REPO_DIR="${ATTENDEE_REPO_DIR:-/opt/attendee}"
RUNNER_SCRIPT_SOURCE="${RUNNER_SCRIPT_SOURCE:-scripts/digitalocean/attendee-bot-runner.sh}"
RUNNER_SERVICE_SOURCE="${RUNNER_SERVICE_SOURCE:-scripts/digitalocean/attendee-bot-runner.service}"
BUILD_RUNTIME_IMAGE="${BUILD_RUNTIME_IMAGE:-false}"
PULL_RUNTIME_IMAGE="${PULL_RUNTIME_IMAGE:-true}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
BOT_RUNTIME_DOCKERFILE="${BOT_RUNTIME_DOCKERFILE:-Dockerfile.bot-runtime}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
BOT_RUNTIME_IMAGE_ALIAS="${BOT_RUNTIME_IMAGE_ALIAS:-attendee-bot-runner:latest}"
DOCKER_LOGOUT_AFTER_DEFAULT="false"
if [[ "$PULL_RUNTIME_IMAGE" == "true" ]]; then
  DOCKER_LOGOUT_AFTER_DEFAULT="true"
fi
DOCKER_LOGOUT_AFTER="${DOCKER_LOGOUT_AFTER:-$DOCKER_LOGOUT_AFTER_DEFAULT}"

if [[ "$BUILD_RUNTIME_IMAGE" == "true" && "$PULL_RUNTIME_IMAGE" == "true" ]]; then
  echo "ERROR: BUILD_RUNTIME_IMAGE and PULL_RUNTIME_IMAGE cannot both be true." >&2
  exit 2
fi
if [[ "$BUILD_RUNTIME_IMAGE" != "true" && "$PULL_RUNTIME_IMAGE" != "true" ]]; then
  echo "ERROR: one of BUILD_RUNTIME_IMAGE / PULL_RUNTIME_IMAGE must be true." >&2
  exit 2
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates cloud-init curl git jq redis-tools rsync

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker

# Optional Docker Hub login. Enabled when DOCKER_USERNAME + DOCKER_TOKEN are
# set in the environment. Using --password-stdin avoids leaking the token via
# the process table. Paired with DOCKER_LOGOUT_AFTER at the end of the script
# so credentials are stripped before the golden image is snapshotted.
DOCKER_REGISTRY="${DOCKER_REGISTRY:-docker.io}"
if [[ -n "${DOCKER_USERNAME:-}" && -n "${DOCKER_TOKEN:-}" ]]; then
  echo "docker login ${DOCKER_REGISTRY} as ${DOCKER_USERNAME}"
  printf '%s' "$DOCKER_TOKEN" \
    | docker login "$DOCKER_REGISTRY" -u "$DOCKER_USERNAME" --password-stdin
fi

# Place source (support scripts, and optionally the Dockerfile for BUILD
# mode) into $ATTENDEE_REPO_DIR. Accept either a git URL or a local path.
if [[ -d "$ATTENDEE_REPO_URL" ]]; then
  echo "ATTENDEE_REPO_URL is a local directory; copying into $ATTENDEE_REPO_DIR"
  mkdir -p "$ATTENDEE_REPO_DIR"
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='.venv/' \
    "$ATTENDEE_REPO_URL"/ "$ATTENDEE_REPO_DIR"/
else
  if [[ ! -d "$ATTENDEE_REPO_DIR/.git" ]]; then
    git clone "$ATTENDEE_REPO_URL" "$ATTENDEE_REPO_DIR"
  fi
  (
    cd "$ATTENDEE_REPO_DIR"
    git fetch --all --tags
    if git show-ref --verify --quiet "refs/remotes/origin/${ATTENDEE_GIT_REF}"; then
      git checkout -B "${ATTENDEE_GIT_REF}" "origin/${ATTENDEE_GIT_REF}"
    else
      git checkout "${ATTENDEE_GIT_REF}"
    fi
  )
fi

cd "$ATTENDEE_REPO_DIR"

if [[ "$BUILD_RUNTIME_IMAGE" == "true" ]]; then
  echo "Building $BOT_RUNTIME_IMAGE from $ATTENDEE_REPO_DIR/$BOT_RUNTIME_DOCKERFILE"
  DOCKER_BUILDKIT=1 docker build \
    --platform "$DOCKER_PLATFORM" \
    -f "$BOT_RUNTIME_DOCKERFILE" \
    -t "$BOT_RUNTIME_IMAGE" \
    .
fi

if [[ "$PULL_RUNTIME_IMAGE" == "true" ]]; then
  echo "Pulling $BOT_RUNTIME_IMAGE"
  docker pull --platform "$DOCKER_PLATFORM" "$BOT_RUNTIME_IMAGE"
fi

docker image inspect "$BOT_RUNTIME_IMAGE" >/dev/null

if [[ -n "$BOT_RUNTIME_IMAGE_ALIAS" && "$BOT_RUNTIME_IMAGE_ALIAS" != "$BOT_RUNTIME_IMAGE" ]]; then
  echo "Tagging $BOT_RUNTIME_IMAGE as $BOT_RUNTIME_IMAGE_ALIAS"
  docker tag "$BOT_RUNTIME_IMAGE" "$BOT_RUNTIME_IMAGE_ALIAS"
fi

RUNTIME_DIGEST="$(docker image inspect --format='{{index .RepoDigests 0}}' "$BOT_RUNTIME_IMAGE" 2>/dev/null || true)"
RUNTIME_IMAGE_ID="$(docker image inspect --format='{{.Id}}' "$BOT_RUNTIME_IMAGE")"
echo "Runtime image ID:     $RUNTIME_IMAGE_ID"
echo "Runtime image digest: ${RUNTIME_DIGEST:-<none>}"

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

# Strip registry credentials before snapshotting so they are not baked into
# the golden image.
if [[ "$DOCKER_LOGOUT_AFTER" == "true" ]]; then
  if [[ -f /root/.docker/config.json ]]; then
    # Extract every registry host configured in the docker CLI auth file.
    REGISTRIES="$(jq -r '(.auths // {}) | keys[]' /root/.docker/config.json 2>/dev/null || true)"
    for reg in $REGISTRIES; do
      echo "docker logout $reg"
      docker logout "$reg" || true
    done
    # Belt & suspenders: remove the file if still present.
    rm -f /root/.docker/config.json
  fi
fi

cloud-init clean --logs
truncate -s 0 /etc/machine-id || true
rm -f /var/lib/dbus/machine-id || true

echo
echo "Golden image builder VM prepared."
echo "Mode: build=${BUILD_RUNTIME_IMAGE} pull=${PULL_RUNTIME_IMAGE}"
echo "Runtime image: ${BOT_RUNTIME_IMAGE}"
echo "Local alias:   ${BOT_RUNTIME_IMAGE_ALIAS}"
echo "Digest:        ${RUNTIME_DIGEST:-<none>}"
echo "Next steps:"
echo "  1. Stop this VM."
echo "  2. Create a custom image from this disk."
echo "  3. Publish or update the image family backing GCP_BOT_SOURCE_IMAGE_FAMILY."
echo "  4. Point BOT_RUNTIME_IMAGE at the pre-pulled runtime image digest baked into this image."
