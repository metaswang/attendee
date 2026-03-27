#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-}"
PLATFORM="${PLATFORM:-linux/amd64}"

if [[ -z "${IMAGE_NAME}" ]]; then
  echo "IMAGE_NAME is required, e.g. IMAGE_NAME=docker.io/<namespace>/attendee-bot:modal-v1" >&2
  exit 1
fi

docker buildx build \
  --platform "${PLATFORM}" \
  -t "${IMAGE_NAME}" \
  --push \
  .
