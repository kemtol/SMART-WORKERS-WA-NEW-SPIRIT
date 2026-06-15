#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-"$ROOT_DIR/migration-exports"}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$OUT_DIR/new-spirit-runtime-$STAMP.tar.gz"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

copy_if_exists() {
  local rel_path="$1"
  local src="$ROOT_DIR/$rel_path"
  local dst="$TMP_DIR/$rel_path"

  if [ -e "$src" ]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    return 0
  fi

  return 1
}

copied=0

if [ -f "$ROOT_DIR/data/ops_messages.sqlite3" ]; then
  mkdir -p "$TMP_DIR/data"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$ROOT_DIR/data/ops_messages.sqlite3" ".backup '$TMP_DIR/data/ops_messages.sqlite3'"
  else
    cp -a "$ROOT_DIR/data/ops_messages.sqlite3" "$TMP_DIR/data/ops_messages.sqlite3"
    copy_if_exists "data/ops_messages.sqlite3-wal" || true
    copy_if_exists "data/ops_messages.sqlite3-shm" || true
  fi
  copied=1
fi

copy_if_exists "data/reference/master_iata.json" && copied=1 || true
copy_if_exists "data/google-sheets-movement-sync-state.json" && copied=1 || true

if [ "${INCLUDE_LOCAL_ENV:-0}" = "1" ]; then
  copy_if_exists "config/google-sheets.env" && copied=1 || true
fi

if [ "$copied" -eq 0 ]; then
  echo "No migration files found. Expected data/ops_messages.sqlite3 and/or data/reference/master_iata.json." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
tar -czf "$ARCHIVE" -C "$TMP_DIR" .

if [ -n "${MIGRATION_PASSPHRASE:-}" ]; then
  openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
    -in "$ARCHIVE" \
    -out "$ARCHIVE.enc" \
    -pass env:MIGRATION_PASSPHRASE
  rm "$ARCHIVE"
  echo "Created encrypted migration archive: $ARCHIVE.enc"
else
  echo "Created migration archive: $ARCHIVE"
  echo "Warning: archive is not encrypted. Set MIGRATION_PASSPHRASE to encrypt it."
fi
