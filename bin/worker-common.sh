#!/usr/bin/env bash

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data"
PID_DIR="$DATA_DIR/pids"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

GROUP_NAME="${WHATSAPP_GROUP_NAME:-New Spirit}"
AUTH_DIR="${WHATSAPP_AUTH_DIR:-.runtime-auth/listener}"
WAIT_TIMEOUT_MS="${WHATSAPP_WAIT_TIMEOUT_MS:-300000}"
RESTART_DELAY_SECONDS="${WHATSAPP_RESTART_DELAY_SECONDS:-10}"
CONNECT_BOOTSTRAP_ATTEMPTS="${CONNECT_BOOTSTRAP_ATTEMPTS:-3}"

SYSTEMD_UNITS=(
  new-spirit-ingest.service
  new-spirit-sheets-sync.service
  new-spirit-afml-sync.service
  new-spirit-listener.service
)

log() {
  printf '%s\n' "$*"
}

auth_path() {
  case "$AUTH_DIR" in
    /*) printf '%s\n' "$AUTH_DIR" ;;
    *) printf '%s\n' "$ROOT_DIR/$AUTH_DIR" ;;
  esac
}

auth_exists() {
  [[ -f "$(auth_path)/creds.json" ]]
}

clear_auth() {
  rm -rf "$(auth_path)" \
    "$DATA_DIR/listener-status.json" \
    "$DATA_DIR/listener-qr.txt" \
    "$DATA_DIR/listener-qr.png"
}

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

write_systemd_unit_files() {
  local bash_bin node_bin python_bin runtime_path
  bash_bin="$(command -v bash)"
  node_bin="$(command -v node)"
  python_bin="$(command -v python3)"
  runtime_path="$(dirname "$node_bin"):$(dirname "$python_bin"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  mkdir -p "$USER_UNIT_DIR"

  cat > "$USER_UNIT_DIR/new-spirit-ingest.service" <<EOF
[Unit]
Description=New Spirit ingest service

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="RESTART_DELAY_SECONDS=$RESTART_DELAY_SECONDS"
Environment="PATH=$runtime_path"
Environment="PYTHON_BIN=$python_bin"
ExecStart=$bash_bin $ROOT_DIR/bin/run-ingest-loop.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

  cat > "$USER_UNIT_DIR/new-spirit-sheets-sync.service" <<EOF
[Unit]
Description=New Spirit Google Sheets sync
After=new-spirit-ingest.service
Wants=new-spirit-ingest.service

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="RESTART_DELAY_SECONDS=$RESTART_DELAY_SECONDS"
Environment="PATH=$runtime_path"
Environment="PYTHON_BIN=$python_bin"
ExecStart=$bash_bin $ROOT_DIR/bin/run-sheets-sync-loop.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

  cat > "$USER_UNIT_DIR/new-spirit-listener.service" <<EOF
[Unit]
Description=New Spirit WhatsApp listener
After=new-spirit-ingest.service
Wants=new-spirit-ingest.service

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="GROUP_NAME=$GROUP_NAME"
Environment="AUTH_DIR=$AUTH_DIR"
Environment="RESTART_DELAY_SECONDS=$RESTART_DELAY_SECONDS"
Environment="PATH=$runtime_path"
Environment="NODE_BIN=$node_bin"
ExecStart=$bash_bin $ROOT_DIR/bin/run-listener-loop.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

  cat > "$USER_UNIT_DIR/new-spirit-afml-sync.service" <<EOF
[Unit]
Description=New Spirit AFML read-only reconciliation
After=new-spirit-ingest.service
Wants=new-spirit-ingest.service

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="RESTART_DELAY_SECONDS=$RESTART_DELAY_SECONDS"
Environment="PATH=$runtime_path"
Environment="PYTHON_BIN=$python_bin"
ExecStart=$bash_bin $ROOT_DIR/bin/run-afml-sync-loop.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
}

enable_linger_if_possible() {
  if ! command -v loginctl >/dev/null 2>&1; then
    log "Peringatan: loginctl tidak tersedia; auto-start saat reboot mungkin tidak aktif."
    return 0
  fi

  if loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -qx 'yes'; then
    return 0
  fi

  if loginctl enable-linger "$USER" >/dev/null 2>&1; then
    log "Linger systemd user diaktifkan untuk user $USER."
  else
    log "Peringatan: gagal mengaktifkan linger otomatis."
    log "Jalankan sekali dari terminal biasa jika butuh tahan reboot tanpa login:"
    log "  sudo loginctl enable-linger $USER"
  fi
}

install_systemd_services() {
  write_systemd_unit_files
  enable_linger_if_possible
  systemctl --user enable "${SYSTEMD_UNITS[@]}" >/dev/null
}

stop_systemd_services() {
  local disable="${1:-0}"
  if ! systemd_user_available; then
    return 0
  fi

  systemctl --user stop "${SYSTEMD_UNITS[@]}" >/dev/null 2>&1 || true
  if [[ "$disable" == "1" ]]; then
    systemctl --user disable "${SYSTEMD_UNITS[@]}" >/dev/null 2>&1 || true
  fi
}

start_systemd_services() {
  install_systemd_services
  systemctl --user restart \
    new-spirit-ingest.service \
    new-spirit-sheets-sync.service \
    new-spirit-afml-sync.service \
    new-spirit-listener.service
}

repo_pids_for_pattern() {
  local pattern="$1"
  local pid cwd
  while read -r pid _; do
    [[ -z "${pid:-}" ]] && continue
    [[ "$pid" == "$$" || "$pid" == "${BASHPID:-$$}" ]] && continue
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    if [[ "$cwd" == "$ROOT_DIR" ]]; then
      printf '%s\n' "$pid"
    fi
  done < <(pgrep -af "$pattern" 2>/dev/null || true)
}

kill_repo_processes() {
  local patterns=(
    'bin/run-listener-loop.sh'
    'bin/run-ingest-loop.sh'
    'bin/run-sheets-sync-loop.sh'
    'bin/run-afml-sync-loop.sh'
    'src/listen-new-messages.js'
    'app/ingest_service.py'
    'app/google_sheets_sync.py'
    'app/afml_sync.py'
  )
  local signal pattern pid

  for signal in TERM KILL; do
    for pattern in "${patterns[@]}"; do
      while read -r pid; do
        [[ -z "${pid:-}" ]] && continue
        kill "-$signal" "$pid" >/dev/null 2>&1 || true
      done < <(repo_pids_for_pattern "$pattern")
    done
    [[ "$signal" == "TERM" ]] && sleep 1
  done

  rm -f "$PID_DIR"/*.pid 2>/dev/null || true
}

start_fallback_worker() {
  local name="$1"
  local script="$2"
  local log_file="$DATA_DIR/$name.log"
  local pid_file="$PID_DIR/$name.pid"

  mkdir -p "$DATA_DIR" "$PID_DIR"
  (
    cd "$ROOT_DIR"
    nohup bash "$script" > "$log_file" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$pid_file"
  )
  log "Started fallback worker $name pid $(cat "$pid_file") log $log_file"
}

start_fallback_workers() {
  start_fallback_worker ingest-loop bin/run-ingest-loop.sh
  start_fallback_worker sheets-sync-loop bin/run-sheets-sync-loop.sh
  start_fallback_worker afml-sync-loop bin/run-afml-sync-loop.sh
  start_fallback_worker listener-loop bin/run-listener-loop.sh
}

bootstrap_whatsapp_connection() {
  local attempt code
  mkdir -p "$DATA_DIR"

  for attempt in $(seq 1 "$CONNECT_BOOTSTRAP_ATTEMPTS"); do
    log "Bootstrap WhatsApp attempt $attempt/$CONNECT_BOOTSTRAP_ATTEMPTS untuk grup: $GROUP_NAME"
    log "Scan QR jika muncul: WhatsApp -> Linked devices -> Link a device"
    log

    if (
      cd "$ROOT_DIR"
      node src/listen-new-messages.js \
        --auth-dir "$AUTH_DIR" \
        --group-name "$GROUP_NAME" \
        --wait-timeout-ms "$WAIT_TIMEOUT_MS" \
        --discover-only "$@"
    ); then
      return 0
    else
      code=$?
    fi

    if auth_exists; then
      log "Auth WhatsApp sudah ada, retry memakai sesi yang sama dalam 3 detik."
    else
      log "Auth WhatsApp belum valid, membersihkan auth sementara sebelum QR baru."
      clear_auth
    fi
    sleep 3
  done

  return 1
}

print_worker_status() {
  log "Repo: $ROOT_DIR"
  log

  if systemd_user_available; then
    log "Systemd user services:"
    local unit active enabled
    for unit in "${SYSTEMD_UNITS[@]}"; do
      active="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
      enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
      printf '  %-30s active=%-10s enabled=%s\n' "$unit" "${active:-unknown}" "${enabled:-unknown}"
    done
  else
    log "Systemd user tidak tersedia di session ini."
  fi

  log
  log "Fallback/manual process dari repo ini:"
  local found=0 pattern pid
  for pattern in 'run-listener-loop.sh' 'run-ingest-loop.sh' 'run-sheets-sync-loop.sh' 'run-afml-sync-loop.sh' 'listen-new-messages.js' 'ingest_service.py' 'google_sheets_sync.py' 'afml_sync.py'; do
    while read -r pid; do
      [[ -z "${pid:-}" ]] && continue
      found=1
      printf '  pid=%s pattern=%s\n' "$pid" "$pattern"
    done < <(repo_pids_for_pattern "$pattern")
  done
  [[ "$found" -eq 0 ]] && log "  tidak ada proses manual/fallback yang aktif"

  log
  log "Ingest health:"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS http://127.0.0.1:8088/health 2>/dev/null || log "  tidak reachable di http://127.0.0.1:8088/health"
  else
    log "  curl tidak tersedia"
  fi
  log

  for file in "$DATA_DIR/listener-status.json" "$DATA_DIR/google-sheets-movement-sync-state.json" "$DATA_DIR/afml-sync-state.json"; do
    log "$(basename "$file"):"
    if [[ -f "$file" ]]; then
      sed -n '1,120p' "$file"
    else
      log "  belum ada"
    fi
    log
  done
}
