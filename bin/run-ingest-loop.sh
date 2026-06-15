#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."
mkdir -p data

RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-10}"

while true; do
  printf '[%s] starting ingest service\n' "$(date -Is)"
  python3 app/ingest_service.py
  code=$?
  printf '[%s] ingest service exited with code %s; restarting in %ss\n' "$(date -Is)" "$code" "$RESTART_DELAY_SECONDS"
  sleep "$RESTART_DELAY_SECONDS"
done
