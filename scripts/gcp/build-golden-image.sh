#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required}"
: "${BOT_RUNTIME_IMAGE:?BOT_RUNTIME_IMAGE is required, for example catblueberry/attendee-bot-runner:latest}"

ZONE="${ZONE:-asia-southeast1-b}"
MACHINE_TYPE="${MACHINE_TYPE:-n2-standard-2}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-20GB}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-pd-balanced}"
BASE_IMAGE="${BASE_IMAGE:-projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts}"
IMAGE_FAMILY="${IMAGE_FAMILY:-attendee-bot-golden}"
IMAGE_NAME="${IMAGE_NAME:-${IMAGE_FAMILY}-$(date -u +%Y%m%d-%H%M)}"
BUILDER_NAME="${BUILDER_NAME:-attendee-golden-builder-$(date -u +%Y%m%d-%H%M)}"
STORAGE_LOCATION="${STORAGE_LOCATION:-asia}"
REMOTE_ARCHIVE="/tmp/voxella-attendee-src.tgz"
REMOTE_REPO_DIR="/opt/voxella-attendee-src"
BOT_RUNTIME_IMAGE_ALIAS="${BOT_RUNTIME_IMAGE_ALIAS:-attendee-bot-runner:latest}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cleanup_files=()
cleanup() {
  for file in "${cleanup_files[@]}"; do
    rm -f "$file"
  done
}
trap cleanup EXIT

archive_file="$(mktemp)"
cleanup_files+=("$archive_file")

echo "Creating source archive from ${REPO_ROOT}"
COPYFILE_DISABLE=1 tar -C "$REPO_ROOT" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='node_modules' \
  -czf "$archive_file" \
  .

echo "Creating attendee golden-image builder ${BUILDER_NAME}"
echo "  project=${GCP_PROJECT_ID}"
echo "  zone=${ZONE}"
echo "  machine=${MACHINE_TYPE}"
echo "  disk=${BOOT_DISK_SIZE}"
echo "  base=${BASE_IMAGE}"
echo "  image=${IMAGE_NAME}"
echo "  family=${IMAGE_FAMILY}"
echo "  runtime_image=${BOT_RUNTIME_IMAGE}"

if [[ "${DRY_RUN:-0}" == "1" || "${DRY_RUN:-}" == "true" ]]; then
  echo "DRY_RUN: gcloud compute instances create ${BUILDER_NAME} ..."
  exit 0
fi

gcloud compute instances create "$BUILDER_NAME" \
  --project "$GCP_PROJECT_ID" \
  --zone "$ZONE" \
  --machine-type "$MACHINE_TYPE" \
  --maintenance-policy TERMINATE \
  --restart-on-failure \
  --boot-disk-size "$BOOT_DISK_SIZE" \
  --boot-disk-type "$BOOT_DISK_TYPE" \
  --image "$BASE_IMAGE" \
  --scopes cloud-platform

echo "Waiting for SSH on ${BUILDER_NAME}..."
for _ in $(seq 1 60); do
  if gcloud compute ssh "$BUILDER_NAME" \
    --project "$GCP_PROJECT_ID" \
    --zone "$ZONE" \
    --command "true" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

gcloud compute scp "$archive_file" "${BUILDER_NAME}:${REMOTE_ARCHIVE}" \
  --project "$GCP_PROJECT_ID" \
  --zone "$ZONE"

remote_env=(
  "ATTENDEE_REPO_URL=${REMOTE_REPO_DIR}"
  "BOT_RUNTIME_IMAGE=${BOT_RUNTIME_IMAGE}"
  "BOT_RUNTIME_IMAGE_ALIAS=${BOT_RUNTIME_IMAGE_ALIAS}"
  "BUILD_RUNTIME_IMAGE=false"
  "PULL_RUNTIME_IMAGE=true"
  "DOCKER_PLATFORM=${DOCKER_PLATFORM}"
  "PYTHON_BIN=${PYTHON_BIN}"
)
if [[ -n "${DOCKER_USERNAME:-}" ]]; then
  remote_env+=("DOCKER_USERNAME=${DOCKER_USERNAME}")
fi
if [[ -n "${DOCKER_TOKEN:-}" ]]; then
  remote_env+=("DOCKER_TOKEN=${DOCKER_TOKEN}")
fi
if [[ -n "${DOCKER_REGISTRY:-}" ]]; then
  remote_env+=("DOCKER_REGISTRY=${DOCKER_REGISTRY}")
fi

printf -v remote_env_prefix '%q ' "${remote_env[@]}"
remote_command=$(cat <<EOF
set -euo pipefail
sudo rm -rf '${REMOTE_REPO_DIR}'
sudo mkdir -p '${REMOTE_REPO_DIR}'
sudo tar -xzf '${REMOTE_ARCHIVE}' -C '${REMOTE_REPO_DIR}'
sudo env ${remote_env_prefix} bash '${REMOTE_REPO_DIR}/scripts/gcp/prepare-golden-image.sh'
sudo poweroff
EOF
)

set +e
gcloud compute ssh "$BUILDER_NAME" \
  --project "$GCP_PROJECT_ID" \
  --zone "$ZONE" \
  --command "$remote_command"
ssh_status=$?
set -e
if [[ "$ssh_status" != "0" ]]; then
  # `sudo poweroff` normally closes SSH with 255 before Compute Engine has
  # changed RUNNING to STOPPING. Allow that control-plane status to converge
  # before treating the preparation as failed.
  current_status=""
  for _ in $(seq 1 12); do
    current_status="$(gcloud compute instances describe "$BUILDER_NAME" --project "$GCP_PROJECT_ID" --zone "$ZONE" --format='value(status)' 2>/dev/null || true)"
    [[ "$current_status" == "STOPPING" || "$current_status" == "TERMINATED" ]] && break
    sleep 5
  done
  if [[ "$current_status" != "STOPPING" && "$current_status" != "TERMINATED" ]]; then
    echo "Remote golden image preparation failed; builder is still ${current_status:-unknown}: ${BUILDER_NAME}" >&2
    exit "$ssh_status"
  fi
fi

echo "Waiting for ${BUILDER_NAME} to stop..."
while true; do
  status="$(gcloud compute instances describe "$BUILDER_NAME" --project "$GCP_PROJECT_ID" --zone "$ZONE" --format='value(status)')"
  [[ "$status" == "TERMINATED" ]] && break
  sleep 10
done

disk_name="$(gcloud compute instances describe "$BUILDER_NAME" --project "$GCP_PROJECT_ID" --zone "$ZONE" --format='value(disks[0].source)' | awk -F/ '{print $NF}')"
gcloud compute images create "$IMAGE_NAME" \
  --project "$GCP_PROJECT_ID" \
  --source-disk "$disk_name" \
  --source-disk-zone "$ZONE" \
  --family "$IMAGE_FAMILY" \
  --storage-location "$STORAGE_LOCATION"

gcloud compute instances delete "$BUILDER_NAME" \
  --project "$GCP_PROJECT_ID" \
  --zone "$ZONE" \
  --quiet

echo "Created attendee golden image: projects/${GCP_PROJECT_ID}/global/images/${IMAGE_NAME}"
echo "Family: projects/${GCP_PROJECT_ID}/global/images/family/${IMAGE_FAMILY}"
