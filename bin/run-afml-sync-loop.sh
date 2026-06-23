#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."
mkdir -p data

for env_file in config/google-sheets.env config/afml.env; do
  if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
AFML_REFRESH_SECONDS="${AFML_REFRESH_SECONDS:-1800}"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-30}"

cleanup() {
  trap - EXIT INT TERM
  kill "${sync_pid:-}" 2>/dev/null || true
  wait "${sync_pid:-}" 2>/dev/null || true
  exit 0
}

trap cleanup EXIT INT TERM

while true; do
  printf '[%s] starting AFML read-only sync\n' "$(date -Is)"
  "$PYTHON_BIN" app/afml_sync.py --sync-sheets &
  sync_pid=$!
  wait "$sync_pid"
  code=$?
  if [ "$code" -eq 0 ]; then
    delay="$AFML_REFRESH_SECONDS"
  else
    delay="$RESTART_DELAY_SECONDS"
  fi
  printf '[%s] AFML sync exited with code %s; next run in %ss\n' "$(date -Is)" "$code" "$delay"
  sleep "$delay"
done
