#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=bin/worker-common.sh
. bin/worker-common.sh

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage:
  ./stop.sh

Behavior:
  - Stop semua systemd user service New Spirit.
  - Disable auto-start saat reboot.
  - Kill proses fallback/manual dari repo ini.
  - Tidak menghapus auth WhatsApp atau database lokal.
EOF
  exit 0
fi

log "Stopping New Spirit worker suite..."
stop_systemd_services 1
kill_repo_processes
log "Stopped. Auto-start systemd user juga sudah disabled."
