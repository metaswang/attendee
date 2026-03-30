#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-meetbot.voxstudio.me}"
SSL_DIR="${2:-deploy/nginx/ssl}"
CRT_PATH="${SSL_DIR}/${DOMAIN}.crt"
KEY_PATH="${SSL_DIR}/${DOMAIN}.key"

mkdir -p "$SSL_DIR"

if [[ -f "$CRT_PATH" && -f "$KEY_PATH" ]]; then
  echo "certificate already exists: $CRT_PATH"
  exit 0
fi

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$KEY_PATH" \
  -out "$CRT_PATH" \
  -days 365 \
  -subj "/CN=${DOMAIN}" \
  -addext "subjectAltName=DNS:${DOMAIN}"

echo "generated self-signed certificate for ${DOMAIN}"
