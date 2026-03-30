#!/usr/bin/env bash
set -euo pipefail

: "${ATTENDEE_REPO_URL:?ATTENDEE_REPO_URL is required}"

ATTENDEE_GIT_REF="${ATTENDEE_GIT_REF:-main}"
ATTENDEE_REPO_DIR="${ATTENDEE_REPO_DIR:-/opt/attendee}"
BOT_RUNTIME_IMAGE="${BOT_RUNTIME_IMAGE:-attendee-bot-runner:latest}"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl git jq

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

docker build -t "$BOT_RUNTIME_IMAGE" .

install -D -m 0755 scripts/digitalocean/attendee-bot-runner.sh /usr/local/bin/attendee-bot-runner
install -D -m 0644 scripts/digitalocean/attendee-bot-runner.service /etc/systemd/system/attendee-bot-runner.service

systemctl daemon-reload
systemctl enable attendee-bot-runner.service
systemctl disable attendee-bot-runner.service || true

cloud-init clean --logs
truncate -s 0 /etc/machine-id || true
rm -f /var/lib/dbus/machine-id || true

echo
echo "Template droplet prepared."
echo "Next step: create a snapshot from this droplet."
