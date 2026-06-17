#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GROUP_NAME="${WHATSAPP_GROUP_NAME:-New Spirit}"
AUTH_DIR="${WHATSAPP_AUTH_DIR:-.runtime-auth/listener}"
RESTART_DELAY_SECONDS="${WHATSAPP_RESTART_DELAY_SECONDS:-3}"
WAIT_TIMEOUT_MS="${WHATSAPP_WAIT_TIMEOUT_MS:-120000}"

auth_exists() {
  [[ -f "$AUTH_DIR/creds.json" ]]
}

clear_incomplete_auth() {
  rm -rf "$AUTH_DIR" data/listener-status.json data/listener-qr.txt data/listener-qr.png
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage:
  ./connect.sh
  ./connect.sh --reset

Env opsional:
  WHATSAPP_GROUP_NAME="New Spirit"
  WHATSAPP_AUTH_DIR=".runtime-auth/listener"
  WHATSAPP_RESTART_DELAY_SECONDS="3"
  WHATSAPP_WAIT_TIMEOUT_MS="120000"
EOF
  exit 0
fi

if [[ "${1:-}" == "--reset" ]]; then
  clear_incomplete_auth
  shift
fi

mkdir -p data

echo "Starting WhatsApp listener for group: $GROUP_NAME"
echo "Scan QR yang muncul dengan:"
echo "  WhatsApp -> Linked devices -> Link a device"
echo

while true; do
  if node src/listen-new-messages.js --auth-dir "$AUTH_DIR" --group-name "$GROUP_NAME" --wait-timeout-ms "$WAIT_TIMEOUT_MS" "$@"; then
    code=0
  else
    code=$?
  fi
  if auth_exists; then
    echo "Auth WhatsApp tersedia. Restart akan memakai sesi yang sama."
  else
    echo "Auth WhatsApp belum tersedia. Restart akan membuat QR baru."
  fi
  echo
  echo "Listener exited with code $code. Restarting in ${RESTART_DELAY_SECONDS}s..."
  echo
  sleep "$RESTART_DELAY_SECONDS"
done
