#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=bin/worker-common.sh
. bin/worker-common.sh

if ! systemd_user_available; then
  log "Systemd user tidak tersedia di session ini."
  log "Gunakan ./connect.sh dari terminal biasa, atau jalankan fallback detached tanpa tahan reboot."
  exit 1
fi

install_systemd_services

log "Systemd user services sudah ditulis dan di-enable:"
for unit in "${SYSTEMD_UNITS[@]}"; do
  printf '  %s\n' "$unit"
done
log
log "Start/restart semua worker dengan:"
log "  ./connect.sh"
