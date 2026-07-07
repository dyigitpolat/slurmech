#!/usr/bin/env bash
# Run on your laptop (or any machine that can reach both sura and xlog1).
# Keeps 127.0.0.1:2222 on the GPU server forwarded to xlog1:22, independent of
# VS Code / Cursor Remote SSH sessions.
set -euo pipefail

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "Run with bash, not sh: bash $(basename "$0") $*"
  exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Do not run this script with sudo."
  echo "It must run as your normal user so autossh uses your ~/.ssh keys and config."
  exit 1
fi

GPU_HOST="${GPU_HOST:-sura.ddns.comp.nus.edu.sg}"
GPU_USER="${GPU_USER:-yigit}"
CLUSTER_HOST="${CLUSTER_HOST:-xlog1}"
CLUSTER_PORT="${CLUSTER_PORT:-22}"
TUNNEL_PORT="${TUNNEL_PORT:-2222}"
SSH_CONFIG="${SSH_CONFIG:-$HOME/.ssh/config}"
SSH_CONFIG_FRAGMENT="${SSH_CONFIG_FRAGMENT:-$HOME/.ssh/config.d/xlog1-bridge}"
BRIDGE_HOST="${BRIDGE_HOST:-sura-xlog1-bridge}"
BRIDGE_IDENTITY_FILE="${BRIDGE_IDENTITY_FILE:-$HOME/.ssh/sura-xlog1-bridge}"
PID_FILE="${PID_FILE:-$HOME/.cache/xlog1-bridge/autossh.pid}"
LOG_FILE="${LOG_FILE:-$HOME/.cache/xlog1-bridge/autossh.log}"

LAUNCHD_LABEL="${LAUNCHD_LABEL:-edu.nus.comp.xlog1-bridge}"
LAUNCHD_PLIST="${LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist}"

abspath() {
  local path="$1"
  case "$path" in
    "~") path="$HOME" ;;
    "~/"*) path="$HOME/${path#~/}" ;;
  esac
  local dir base
  dir="$(dirname "$path")"
  base="$(basename "$path")"
  echo "$(cd "$dir" 2>/dev/null && pwd)/${base}"
}

autossh_pgrep_pattern() {
  echo "autossh.*(${BRIDGE_HOST}|${GPU_HOST}|${TUNNEL_PORT}:${CLUSTER_HOST})"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") {setup-key|verify-key|start|stop|status|restart|logs|install-launchd|uninstall-launchd}

Persistent reverse SSH tunnel:
  sura:${TUNNEL_PORT} -> ${CLUSTER_HOST}:${CLUSTER_PORT}

Environment overrides:
  GPU_HOST GPU_USER CLUSTER_HOST CLUSTER_PORT TUNNEL_PORT
  SSH_CONFIG BRIDGE_HOST BRIDGE_IDENTITY_FILE PID_FILE LOG_FILE

First-time setup:
  1. bash $(basename "$0") setup-key
  2. Copy the public key to sura and xlog1 (commands printed by setup-key)
  3. bash $(basename "$0") verify-key
  4. bash $(basename "$0") start
  5. Remove RemoteForward ${TUNNEL_PORT} from your VS Code/Cursor SSH config

launchd requires setup-key (no ssh-agent in background services):
  bash $(basename "$0") setup-key
  bash $(basename "$0") install-launchd
EOF
}

ensure_writable_cache() {
  local cache_dir
  cache_dir="$(dirname "$LOG_FILE")"
  mkdir -p "$cache_dir" 2>/dev/null || true
  if [[ -e "$LOG_FILE" && ! -w "$LOG_FILE" ]] || [[ ! -w "$cache_dir" ]]; then
    echo "Cannot write to ${cache_dir} (likely created by an earlier sudo run)."
    echo "Fix ownership, then retry:"
    echo "  sudo chown -R $(id -un):$(id -gn) ${cache_dir}"
    exit 1
  fi
  : >>"$LOG_FILE"
}

remove_config_block() {
  local marker="$1"
  local file="$2"
  [[ -f "$file" ]] || return 0
  local begin="# BEGIN ${marker}"
  local end="# END ${marker}"
  awk -v begin="$begin" -v end="$end" '
    $0 == begin { skip=1; next }
    $0 == end { skip=0; next }
    !skip { print }
  ' "$file" >"${file}.tmp" && mv "${file}.tmp" "$file"
}

ensure_ssh_include() {
  mkdir -p "$(dirname "$SSH_CONFIG")" "$(dirname "$SSH_CONFIG_FRAGMENT")"
  local include_line="Include ${SSH_CONFIG_FRAGMENT}"
  if [[ -f "$SSH_CONFIG" ]]; then
    if ! grep -Fq "$include_line" "$SSH_CONFIG" 2>/dev/null; then
      { echo "$include_line"; cat "$SSH_CONFIG"; } >"${SSH_CONFIG}.tmp"
      mv "${SSH_CONFIG}.tmp" "$SSH_CONFIG"
    fi
  else
    echo "$include_line" >"$SSH_CONFIG"
  fi
}

write_bridge_host_block() {
  ensure_ssh_include
  cat >"$SSH_CONFIG_FRAGMENT" <<EOF
# Managed by xlog1-bridge-local.sh
Host ${BRIDGE_HOST}
  HostName ${GPU_HOST}
  User ${GPU_USER}
  IdentityFile ${BRIDGE_IDENTITY_FILE}
  IdentitiesOnly yes
$( [[ "$(uname -s)" == "Darwin" ]] && echo "  UseKeychain yes" )
  RemoteForward 127.0.0.1:${TUNNEL_PORT} ${CLUSTER_HOST}:${CLUSTER_PORT}
  ExitOnForwardFailure yes
  ServerAliveInterval 30
  ServerAliveCountMax 3
  StrictHostKeyChecking accept-new

Host ${CLUSTER_HOST}
  User ${GPU_USER}
  IdentityFile ${BRIDGE_IDENTITY_FILE}
  IdentitiesOnly yes
$( [[ "$(uname -s)" == "Darwin" ]] && echo "  UseKeychain yes" )
EOF
}

ensure_ssh_config() {
  ensure_writable_cache
  mkdir -p "$(dirname "$PID_FILE")"
  if [[ ! -f "$SSH_CONFIG_FRAGMENT" ]] \
    || ! grep -q "IdentityFile ${BRIDGE_IDENTITY_FILE}" "$SSH_CONFIG_FRAGMENT" 2>/dev/null; then
    write_bridge_host_block
    echo "Updated ${SSH_CONFIG_FRAGMENT}"
  fi
  ensure_ssh_include
}

setup_key() {
  ensure_writable_cache
  mkdir -p "$(dirname "$BRIDGE_IDENTITY_FILE")"
  if [[ ! -f "${BRIDGE_IDENTITY_FILE}" ]]; then
    ssh-keygen -t ed25519 -f "${BRIDGE_IDENTITY_FILE}" -N "" -C "xlog1-bridge@$(hostname -s 2>/dev/null || hostname)"
    echo "Created ${BRIDGE_IDENTITY_FILE}"
  else
    echo "Key already exists: ${BRIDGE_IDENTITY_FILE}"
  fi
  write_bridge_host_block
  cat <<EOF

Add this public key to BOTH sura and xlog1 (one-time):

  $(cat "${BRIDGE_IDENTITY_FILE}.pub")

Commands (ClearAllForwardings avoids conflict with an existing 2222 bridge):

  ssh-copy-id -o ClearAllForwardings=yes -i ${BRIDGE_IDENTITY_FILE}.pub ${GPU_USER}@${GPU_HOST}
  ssh-copy-id -o ClearAllForwardings=yes -i ${BRIDGE_IDENTITY_FILE}.pub ${GPU_USER}@${CLUSTER_HOST}

Then verify:

  bash $(basename "$0") verify-key
EOF
}

verify_key() {
  ensure_ssh_config
  local failed=0
  local ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ClearAllForwardings=yes
    -i "${BRIDGE_IDENTITY_FILE}" -o IdentitiesOnly=yes)
  if ssh "${ssh_opts[@]}" "${GPU_USER}@${GPU_HOST}" true; then
    echo "OK: key auth to ${GPU_USER}@${GPU_HOST}"
  else
    echo "FAIL: cannot reach ${GPU_USER}@${GPU_HOST} with ${BRIDGE_IDENTITY_FILE}"
    failed=1
  fi
  if ssh "${ssh_opts[@]}" -F "$SSH_CONFIG" "${GPU_USER}@${CLUSTER_HOST}" true; then
    echo "OK: key auth to ${GPU_USER}@${CLUSTER_HOST}"
  else
    echo "FAIL: cannot reach ${GPU_USER}@${CLUSTER_HOST} with ${BRIDGE_IDENTITY_FILE}"
    failed=1
  fi
  return "$failed"
}

require_launchd_key() {
  if [[ ! -f "${BRIDGE_IDENTITY_FILE}" ]]; then
    echo "launchd cannot use your ssh-agent. Run setup-key first:"
    echo "  bash $(basename "$0") setup-key"
    exit 1
  fi
  if ! verify_key; then
    echo "Fix key auth before install-launchd (see ssh-copy-id commands from setup-key)."
    exit 1
  fi
}

preflight_forward() {
  echo "Testing reverse forward sura:${TUNNEL_PORT} -> ${CLUSTER_HOST}:${CLUSTER_PORT}..."
  local probe_pid err_log
  err_log="$(mktemp)"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=15 -o ExitOnForwardFailure=yes \
    -i "${BRIDGE_IDENTITY_FILE}" -o IdentitiesOnly=yes \
    -R "127.0.0.1:${TUNNEL_PORT}:${CLUSTER_HOST}:${CLUSTER_PORT}" \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -f -N "${GPU_USER}@${GPU_HOST}" 2>"$err_log"; then
    echo "Cannot bind port ${TUNNEL_PORT} on sura."
    [[ -s "$err_log" ]] && sed -n '1,3p' "$err_log"
    echo ""
    echo "Another SSH session (usually Cursor/VS Code Remote SSH) already holds port ${TUNNEL_PORT}."
    echo "Disconnect Remote SSH to sura, or remove RemoteForward ${TUNNEL_PORT} from ~/.ssh/config."
    rm -f "$err_log"
    exit 1
  fi
  sleep 1
  probe_pid="$(pgrep -f "ssh.*127.0.0.1:${TUNNEL_PORT}:${CLUSTER_HOST}:${CLUSTER_PORT}" | head -1 || true)"
  [[ -n "$probe_pid" ]] && kill "$probe_pid" 2>/dev/null || true
  pkill -f "ssh.*${GPU_HOST}.*127.0.0.1:${TUNNEL_PORT}" 2>/dev/null || true
  rm -f "$err_log"
  echo "Forward test OK."
}

show_launchd_failure() {
  local domain="$1"
  echo ""
  echo "--- ${LOG_FILE} (last 15 lines) ---"
  tail -15 "$LOG_FILE" 2>/dev/null || echo "(empty or missing)"
  echo "--- launchctl print ${domain}/${LAUNCHD_LABEL} ---"
  launchctl print "${domain}/${LAUNCHD_LABEL}" 2>/dev/null | tail -20 || true
}

require_autossh() {
  if ! command -v autossh >/dev/null 2>&1; then
    echo "autossh not found. Install it, e.g.:"
    echo "  Ubuntu/Debian: sudo apt install autossh"
    echo "  macOS:         brew install autossh"
    exit 1
  fi
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && return 0
  fi
  pgrep -f "$(autossh_pgrep_pattern)" >/dev/null 2>&1
}

running_pid() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
  fi
  pgrep -f "$(autossh_pgrep_pattern)" | head -1
}

start_bridge() {
  require_autossh
  ensure_ssh_config
  if is_running; then
    echo "Bridge already running (pid $(running_pid))."
    exit 0
  fi
  # -M 0 disables autossh's extra monitor port (OpenSSH keepalives are enough).
  autossh -M 0 -f -N \
    -o "ExitOnForwardFailure=yes" \
    -F "$SSH_CONFIG" \
    "$BRIDGE_HOST" >>"$LOG_FILE" 2>&1
  sleep 1
  pgrep -f "$(autossh_pgrep_pattern)" | head -1 >"$PID_FILE" || true
  if is_running; then
    echo "Bridge started (pid $(running_pid))."
    echo "On sura, test with: nc -vz 127.0.0.1 ${TUNNEL_PORT}"
  else
    echo "Bridge failed to start. See ${LOG_FILE}"
    exit 1
  fi
}

stop_bridge() {
  if ! is_running; then
    rm -f "$PID_FILE"
    pkill -f "$(autossh_pgrep_pattern)" 2>/dev/null || true
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  pkill -f "$(autossh_pgrep_pattern)" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Bridge stopped."
}

status_bridge() {
  if is_running; then
    echo "running (pid $(running_pid))"
    echo "route: ${GPU_HOST}:${TUNNEL_PORT} -> ${CLUSTER_HOST}:${CLUSTER_PORT}"
  else
    echo "stopped"
    exit 1
  fi
}

install_launchd() {
  require_autossh
  require_launchd_key
  ensure_ssh_config
  preflight_forward
  stop_bridge
  mkdir -p "$(dirname "$LAUNCHD_PLIST")" "$(dirname "$LOG_FILE")"
  local autossh_bin uid domain session_type=""
  local home_dir identity_file log_file plist_path
  autossh_bin="$(command -v autossh)"
  home_dir="$(abspath "$HOME")"
  identity_file="$(abspath "$BRIDGE_IDENTITY_FILE")"
  log_file="$(abspath "$LOG_FILE")"
  plist_path="$(abspath "$LAUNCHD_PLIST")"
  uid="$(id -u)"
  if launchctl print "gui/${uid}" >/dev/null 2>&1; then
    domain="gui/${uid}"
  elif launchctl print "user/${uid}" >/dev/null 2>&1; then
    domain="user/${uid}"
    session_type="Background"
  else
    echo "No launchd domain available."
    echo "Run install-launchd from Terminal.app on your Mac (not over SSH),"
    echo "or skip launchd and use: $(basename "$0") start"
    exit 1
  fi
  cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${autossh_bin}</string>
    <string>-M</string>
    <string>0</string>
    <string>-N</string>
    <string>-o</string>
    <string>ExitOnForwardFailure=yes</string>
    <string>-o</string>
    <string>ServerAliveInterval=30</string>
    <string>-o</string>
    <string>ServerAliveCountMax=3</string>
    <string>-o</string>
    <string>StrictHostKeyChecking=accept-new</string>
    <string>-i</string>
    <string>${identity_file}</string>
    <string>-o</string>
    <string>IdentitiesOnly=yes</string>
    <string>-R</string>
    <string>127.0.0.1:${TUNNEL_PORT}:${CLUSTER_HOST}:${CLUSTER_PORT}</string>
    <string>${GPU_USER}@${GPU_HOST}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${home_dir}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>${home_dir}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${log_file}</string>
  <key>StandardErrorPath</key>
  <string>${log_file}</string>
EOF
  if [[ -n "$session_type" ]]; then
    cat >>"$plist_path" <<EOF
  <key>LimitLoadToSessionType</key>
  <string>${session_type}</string>
EOF
  fi
  cat >>"$plist_path" <<EOF
</dict>
</plist>
EOF
  launchctl bootout "$domain" "$plist_path" 2>/dev/null \
    || launchctl bootout "$domain/${LAUNCHD_LABEL}" 2>/dev/null \
    || true
  if ! launchctl bootstrap "$domain" "$plist_path"; then
    echo "bootstrap failed for ${domain}; trying legacy launchctl load..."
    launchctl unload "$plist_path" 2>/dev/null || true
    launchctl load -w "$plist_path"
  fi
  sleep 4
  if pgrep -f "$(autossh_pgrep_pattern)" >/dev/null; then
    pgrep -f "$(autossh_pgrep_pattern)" | head -1 >"$PID_FILE" || true
    echo "launchd service installed and running (${domain}): ${plist_path}"
  else
    echo "launchd service installed but autossh is not running yet."
    show_launchd_failure "$domain"
    exit 1
  fi
}

uninstall_launchd() {
  local uid domain
  uid="$(id -u)"
  for domain in "gui/${uid}" "user/${uid}"; do
    launchctl bootout "$domain" "$LAUNCHD_PLIST" 2>/dev/null \
      || launchctl bootout "$domain/${LAUNCHD_LABEL}" 2>/dev/null \
      || true
  done
  launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
  rm -f "$LAUNCHD_PLIST"
  stop_bridge
  echo "launchd service removed."
}

cmd="${1:-}"
case "$cmd" in
  setup-key) setup_key ;;
  verify-key) verify_key ;;
  start) start_bridge ;;
  stop)
    if is_running; then
      stop_bridge
    else
      stop_bridge
      echo "Bridge not running."
    fi
    ;;
  status) status_bridge ;;
  restart) stop_bridge; start_bridge ;;
  logs) tail -f "$LOG_FILE" ;;
  install-launchd) install_launchd ;;
  uninstall-launchd) uninstall_launchd ;;
  -h|--help|help|"") usage ;;
  *) echo "Unknown command: $cmd"; usage; exit 1 ;;
esac
