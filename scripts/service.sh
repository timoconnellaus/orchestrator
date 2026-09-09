#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$ROOT/.data/logs"
LABEL_PREFIX="dev.tim.orchestrator"
DOMAIN="gui/$(id -u)"
MODES="control livekit voice"

usage() {
  cat <<'EOF'
Usage: sh scripts/service.sh COMMAND

Commands:
  install    Install dependencies, install the LaunchAgents, and start them
  update     Refresh dependencies/assets and reload the installed LaunchAgents
  start      Load and start installed LaunchAgents
  stop       Stop and unload LaunchAgents (plist files remain installed)
  restart    Restart loaded LaunchAgents without reinstalling dependencies
  status     Show whether each LaunchAgent is loaded and running
  logs       Show the latest service output
  uninstall  Stop the LaunchAgents and remove their plist files
EOF
}

fail() {
  echo "service.sh: $*" >&2
  exit 1
}

require_macos() {
  [ "$(uname -s)" = "Darwin" ] || fail "launchd setup is supported only on macOS"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found on PATH: $1"
}

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\\&apos;/g"
}

label_for() {
  printf '%s.%s' "$LABEL_PREFIX" "$1"
}

plist_for() {
  printf '%s/%s.plist' "$PLIST_DIR" "$(label_for "$1")"
}

service_for() {
  printf '%s/%s' "$DOMAIN" "$(label_for "$1")"
}

validate_environment() {
  node --env-file="$ROOT/.env" <<'NODE'
const required = ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY"];
const missing = required.filter((name) => !process.env[name]);
if (missing.length) {
  console.error(`Set ${missing.join(", ")} in .env before installing all services.`);
  process.exit(1);
}
NODE
}

prepare() {
  require_command node
  require_command npm
  require_command uv
  require_command livekit-server

  node <<'NODE'
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 13)) {
  console.error(`Node 22.13+ is required; found ${process.versions.node}.`);
  process.exit(1);
}
NODE
  node "$ROOT/scripts/init-local.mjs"
  validate_environment

  echo "Installing service dependencies..."
  (cd "$ROOT/apps/control" && npm ci --no-audit --no-fund)
  (cd "$ROOT/packages/worker-tools" && npm ci --no-audit --no-fund)
  (cd "$ROOT/apps/voice" && uv sync --frozen)
  node "$ROOT/scripts/run.mjs" voice-download
}

write_plist() {
  mode=$1
  label=$(label_for "$mode")
  plist=$(plist_for "$mode")
  temporary="$plist.tmp.$$"
  escaped_label=$(xml_escape "$label")
  escaped_root=$(xml_escape "$ROOT")
  escaped_runner=$(xml_escape "$ROOT/scripts/launchd-run.sh")
  escaped_path=$(xml_escape "$PATH")
  escaped_stdout=$(xml_escape "$LOG_DIR/$mode.out.log")
  escaped_stderr=$(xml_escape "$LOG_DIR/$mode.error.log")

  cat >"$temporary" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$escaped_label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$escaped_runner</string>
    <string>$mode</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$escaped_root</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$escaped_path</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>Umask</key>
  <integer>63</integer>
  <key>ExitTimeOut</key>
  <integer>20</integer>
  <key>StandardOutPath</key>
  <string>$escaped_stdout</string>
  <key>StandardErrorPath</key>
  <string>$escaped_stderr</string>
</dict>
</plist>
EOF
  plutil -lint "$temporary" >/dev/null
  chmod 600 "$temporary"
  mv "$temporary" "$plist"
}

unload_mode() {
  mode=$1
  launchctl bootout "$(service_for "$mode")" >/dev/null 2>&1 || true
}

load_mode() {
  mode=$1
  plist=$(plist_for "$mode")
  [ -f "$plist" ] || fail "missing $plist; run install first"
  launchctl enable "$(service_for "$mode")"
  launchctl bootstrap "$DOMAIN" "$plist"
}

install_agents() {
  mkdir -p "$PLIST_DIR" "$LOG_DIR"
  chmod 700 "$LOG_DIR"

  for mode in $MODES; do
    unload_mode "$mode"
    write_plist "$mode"
    load_mode "$mode"
  done
  echo "Installed and started Orchestrator LaunchAgents."
  echo "They will start automatically after you log in following a restart."
}

start_agents() {
  for mode in $MODES; do
    if launchctl print "$(service_for "$mode")" >/dev/null 2>&1; then
      launchctl kickstart -k "$(service_for "$mode")"
    else
      load_mode "$mode"
    fi
  done
  echo "Started Orchestrator LaunchAgents."
}

stop_agents() {
  for mode in $MODES; do
    unload_mode "$mode"
  done
  echo "Stopped Orchestrator LaunchAgents."
}

status_agents() {
  result=0
  for mode in $MODES; do
    service=$(service_for "$mode")
    if details=$(launchctl print "$service" 2>/dev/null); then
      state=$(printf '%s\n' "$details" | awk -F'= ' '/^[[:space:]]*state = / { print $2; exit }')
      pid=$(printf '%s\n' "$details" | awk -F'= ' '/^[[:space:]]*pid = / { print $2; exit }')
      printf '%-8s loaded, state=%s' "$mode" "${state:-unknown}"
      [ -z "$pid" ] || printf ', pid=%s' "$pid"
      printf '\n'
    else
      printf '%-8s not loaded\n' "$mode"
      result=1
    fi
  done
  return "$result"
}

show_logs() {
  for mode in $MODES; do
    for stream in out error; do
      file="$LOG_DIR/$mode.$stream.log"
      echo "===== $file ====="
      if [ -f "$file" ]; then
        tail -n 40 "$file"
      else
        echo "(no log yet)"
      fi
    done
  done
}

uninstall_agents() {
  stop_agents
  for mode in $MODES; do
    rm -f "$(plist_for "$mode")"
  done
  echo "Removed Orchestrator LaunchAgent plist files. Logs and application data were preserved."
}

require_macos
command=${1:-}
case "$command" in
install)
  prepare
  install_agents
  ;;
update)
  prepare
  install_agents
  ;;
start) start_agents ;;
stop) stop_agents ;;
restart) start_agents ;;
status) status_agents ;;
logs) show_logs ;;
uninstall) uninstall_agents ;;
-h | --help | help) usage ;;
*)
  usage >&2
  exit 2
  ;;
esac
