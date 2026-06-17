#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GROUP_NAME="${WHATSAPP_GROUP_NAME:-New Spirit}"
AUTH_DIR="${WHATSAPP_AUTH_DIR:-.runtime-auth/listener}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage:
  ./connect.sh
  ./connect.sh --reset

Env opsional:
  WHATSAPP_GROUP_NAME="New Spirit"
  WHATSAPP_AUTH_DIR=".runtime-auth/listener"
EOF
  exit 0
fi

if [[ "${1:-}" == "--reset" ]]; then
  rm -rf "$AUTH_DIR" data/listener-status.json data/listener-qr.txt data/listener-qr.png
  shift
elif [[ -f "$AUTH_DIR/creds.json" ]]; then
  registered="$(
    node -e "try { const c = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8')); process.stdout.write(c.registered ? 'yes' : 'no'); } catch { process.stdout.write('no'); }" "$AUTH_DIR/creds.json"
  )"
  if [[ "$registered" != "yes" ]]; then
    rm -rf "$AUTH_DIR" data/listener-status.json data/listener-qr.txt data/listener-qr.png
  fi
fi

mkdir -p data

echo "Starting WhatsApp listener for group: $GROUP_NAME"
echo "Scan QR yang muncul dengan:"
echo "  WhatsApp -> Linked devices -> Link a device"
echo

exec node src/listen-new-messages.js --group-name "$GROUP_NAME" "$@"
