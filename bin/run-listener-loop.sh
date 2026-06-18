#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."
mkdir -p data

AUTH_DIR="${AUTH_DIR:-.runtime-auth/listener}"
GROUP_NAME="${GROUP_NAME:-New Spirit}"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-10}"
NODE_BIN="${NODE_BIN:-node}"

while true; do
  printf '[%s] starting WhatsApp listener for group "%s"\n' "$(date -Is)" "$GROUP_NAME"
  "$NODE_BIN" src/listen-new-messages.js --auth-dir "$AUTH_DIR" --group-name "$GROUP_NAME"
  code=$?
  printf '[%s] listener exited with code %s; restarting in %ss\n' "$(date -Is)" "$code" "$RESTART_DELAY_SECONDS"
  sleep "$RESTART_DELAY_SECONDS"
done
