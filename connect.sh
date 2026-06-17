#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GROUP_NAME="${WHATSAPP_GROUP_NAME:-New Spirit}"
AUTH_DIR="${WHATSAPP_AUTH_DIR:-.runtime-auth/listener}"
RESTART_DELAY_SECONDS="${WHATSAPP_RESTART_DELAY_SECONDS:-3}"
WAIT_TIMEOUT_MS="${WHATSAPP_WAIT_TIMEOUT_MS:-120000}"

auth_registered() {
  if [[ ! -f "$AUTH_DIR/creds.json" ]]; then
    echo no
    return
  fi
  node -e "try { const c = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8')); process.stdout.write(c.registered ? 'yes' : 'no'); } catch { process.stdout.write('no'); }" "$AUTH_DIR/creds.json"
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
elif [[ -f "$AUTH_DIR/creds.json" ]]; then
  if [[ "$(auth_registered)" != "yes" ]]; then
    clear_incomplete_auth
  fi
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
  if [[ "$(auth_registered)" != "yes" ]]; then
    echo "Login belum selesai. Auth sementara dibersihkan; QR baru akan dibuat."
    clear_incomplete_auth
  fi
  echo
  echo "Listener exited with code $code. Restarting in ${RESTART_DELAY_SECONDS}s..."
  echo
  sleep "$RESTART_DELAY_SECONDS"
done
