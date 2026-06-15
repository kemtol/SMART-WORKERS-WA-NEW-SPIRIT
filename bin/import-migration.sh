#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /path/to/new-spirit-runtime-YYYYMMDDTHHMMSSZ.tar.gz[.enc]" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT="$1"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

ARCHIVE="$TMP_DIR/archive.tar.gz"

case "$INPUT" in
  *.enc)
    if [ -z "${MIGRATION_PASSPHRASE:-}" ]; then
      echo "MIGRATION_PASSPHRASE is required to import encrypted archives." >&2
      exit 1
    fi
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
      -in "$INPUT" \
      -out "$ARCHIVE" \
      -pass env:MIGRATION_PASSPHRASE
    ;;
  *)
    cp "$INPUT" "$ARCHIVE"
    ;;
esac

tar -xzf "$ARCHIVE" -C "$ROOT_DIR"
echo "Imported migration archive into: $ROOT_DIR"
