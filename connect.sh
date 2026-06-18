#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=bin/worker-common.sh
. bin/worker-common.sh

RESET=0
EXTRA_ARGS=()

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --reset)
      RESET=1
      shift
      ;;
    --help|-h)
      cat <<EOF
Usage:
  ./connect.sh
  ./connect.sh --reset

Behavior:
  - Stop/kill worker lama dari repo ini.
  - Jika belum login atau --reset dipakai, tampilkan QR di terminal.
  - Setelah login valid, start semua worker:
      ingest service
      Google Sheets sync
      WhatsApp listener
  - Jika systemd user tersedia, worker dibuat tahan reboot.
  - Jika systemd user tidak tersedia, worker tetap detach dari terminal tetapi tidak tahan reboot.

Env opsional:
  WHATSAPP_GROUP_NAME="New Spirit"
  WHATSAPP_AUTH_DIR=".runtime-auth/listener"
  WHATSAPP_WAIT_TIMEOUT_MS="300000"
  WHATSAPP_RESTART_DELAY_SECONDS="10"
  CONNECT_BOOTSTRAP_ATTEMPTS="3"
EOF
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

mkdir -p "$DATA_DIR"

log "Stopping worker lama sebelum start ulang..."
stop_systemd_services 0
kill_repo_processes

if [[ "$RESET" == "1" ]]; then
  log "Reset auth WhatsApp listener..."
  clear_auth
fi

if ! bootstrap_whatsapp_connection "${EXTRA_ARGS[@]}"; then
  log "Gagal validasi koneksi WhatsApp. Worker tidak distart."
  log "Coba ulang dengan:"
  log "  ./connect.sh --reset"
  exit 1
fi

log
if systemd_user_available; then
  log "Systemd user tersedia. Menulis service, enable auto-start, lalu start semua worker..."
  start_systemd_services
  log "Worker aktif via systemd user dan akan auto-start setelah reboot jika linger aktif."
else
  log "Systemd user tidak tersedia. Menjalankan fallback detached background."
  log "Catatan: fallback ini tahan terminal mati, tetapi tidak tahan reboot."
  start_fallback_workers
fi

log
print_worker_status
