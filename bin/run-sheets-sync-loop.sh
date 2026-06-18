#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."
mkdir -p data

if [ -f config/google-sheets.env ]; then
  set -a
  # shellcheck disable=SC1091
  . config/google-sheets.env
  set +a
fi

RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-10}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

while true; do
  printf '[%s] starting Google Sheets sync\n' "$(date -Is)"
  "$PYTHON_BIN" app/google_sheets_sync.py
  code=$?
  printf '[%s] Google Sheets sync exited with code %s; restarting in %ss\n' "$(date -Is)" "$code" "$RESTART_DELAY_SECONDS"
  sleep "$RESTART_DELAY_SECONDS"
done
