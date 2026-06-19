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
MASTER_IATA_REFRESH_SECONDS="${MASTER_IATA_REFRESH_SECONDS:-1800}"
MASTER_IATA_AUTO_SYNC="${MASTER_IATA_AUTO_SYNC:-1}"

sync_master_iata_loop() {
  while true; do
    printf '[%s] refreshing MASTER_IATA\n' "$(date -Is)"
    "$PYTHON_BIN" app/sync_master_iata_sheet.py
    code=$?
    printf '[%s] MASTER_IATA refresh exited with code %s; next run in %ss\n' "$(date -Is)" "$code" "$MASTER_IATA_REFRESH_SECONDS"
    sleep "$MASTER_IATA_REFRESH_SECONDS"
  done
}

if [ "$MASTER_IATA_AUTO_SYNC" != "0" ]; then
  sync_master_iata_loop &
  master_iata_pid=$!
  trap 'kill "$master_iata_pid" 2>/dev/null || true' EXIT INT TERM
fi

while true; do
  printf '[%s] starting Google Sheets sync\n' "$(date -Is)"
  "$PYTHON_BIN" app/google_sheets_sync.py
  code=$?
  printf '[%s] Google Sheets sync exited with code %s; restarting in %ss\n' "$(date -Is)" "$code" "$RESTART_DELAY_SECONDS"
  sleep "$RESTART_DELAY_SECONDS"
done
