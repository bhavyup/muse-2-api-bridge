#!/usr/bin/env bash
# jarvis-serve installer. Idempotent: safe to re-run. Run from the repo root
# (or anywhere; it resolves its own location).
#
# Does (server side):
#   1. checks python >= 3.9
#   2. creates data/, state/, logs/
#   3. generates config.json + bearer token on first run (kept on re-run;
#      pass --rotate-token to regenerate)
#   4. installs + starts the systemd service, verifying it is actually active
#
# Does NOT (agent side, done by the installing agent via its tools):
#   - copying hooks/queue-watch.sh to ~/hooks/scripts/
#   - registering/enabling the hook (see agent/SKILL.md)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/data"; STATE="$ROOT/state"; LOGS="$ROOT/logs"
CONFIG="$ROOT/config.json"; TOKEN_FILE="$ROOT/.token"
ROTATE=0
for a in "$@"; do [ "$a" = "--rotate-token" ] && ROTATE=1; done

fail() { echo "install: ERROR: $*" >&2; exit 1; }
info() { echo "install: $*"; }

# --- 1. python ------------------------------------------------------------
command -v python3 >/dev/null || fail "python3 not found"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" \
  || fail "python3 >= 3.9 required"

# --- 2. directories --------------------------------------------------------
mkdir -p "$DATA/queue" "$DATA/processing" "$DATA/responses" "$DATA/failed" \
         "$STATE" "$LOGS"
info "directories ok"

# --- 3. config + token ------------------------------------------------------
if [ ! -f "$CONFIG" ] || [ "$ROTATE" = "1" ]; then
  [ "$ROTATE" = "1" ] && info "rotating token"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  SHA="$(printf '%s' "$TOKEN" | python3 -c \
    'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())')"
  BIND="127.0.0.1"
  if TSIP="$(tailscale ip -4 2>/dev/null | head -n1)"; then
    if [ -n "$TSIP" ]; then BIND="$TSIP"; info "tailscale detected: $TSIP"; fi
  fi
  [ "$BIND" = "127.0.0.1" ] && info "no tailscale IP found; binding 127.0.0.1 (edit config.json to expose)"
  BIND="$BIND" SHA="$SHA" DATA="$DATA" LOGS="$LOGS" python3 - "$CONFIG" <<'EOF'
import json, os, sys
p = sys.argv[1]
cfg = {
  "bind": os.environ["BIND"], "port": 8765,
  "token_sha256": os.environ["SHA"], "model": "muse-agent",
  "data_dir": os.environ["DATA"], "request_timeout_secs": 300,
  "log_file": os.environ["LOGS"] + "/server.log",
  # Persistent sessions (all optional; these are the defaults):
  "project_name": "jarvis-serve",
  "agent_name": "assistant",
  "session_id_header": "X-Session-Id",
  "session_id_field": None,
  "recent_window_secs": 120,
  "session_idle_secs": 86400,
  "tool_desc_max_chars": 200,
  "seed_template": None,
  "log_wire_keys": True,
}
with open(p, "w") as f: json.dump(cfg, f, indent=2)
EOF
  chmod 600 "$CONFIG"
  printf '%s' "$TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
  echo
  echo "=== SAVE THIS TOKEN (shown once; also in $TOKEN_FILE) ==="
  echo "$TOKEN"
  echo "=========================================================="
  echo
else
  info "config.json exists; keeping (use --rotate-token to regenerate)"
fi

# --- 4. systemd -------------------------------------------------------------
USER_NAME="$(id -un)"
UNIT_SRC="$ROOT/systemd/jarvis-serve.service.template"
UNIT_TMP="$ROOT/systemd/jarvis-serve.service"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$USER_NAME|g" \
    -e "s|@PYTHON@|$(command -v python3)|g" "$UNIT_SRC" > "$UNIT_TMP"

installed=0
if [ "$USER_NAME" = "root" ] && systemctl --version >/dev/null 2>&1; then
  cp "$UNIT_TMP" /etc/systemd/system/jarvis-serve.service
  systemctl daemon-reload
  systemctl enable --now jarvis-serve.service >/dev/null 2>&1 || true
  sleep 2
  if systemctl is-active --quiet jarvis-serve.service; then
    info "systemd service active (system)"
    installed=1
  else
    info "systemd system unit installed but not active; see below"
    systemctl status jarvis-serve.service --no-pager 2>&1 | head -20 || true
  fi
elif systemctl --user --version >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cp "$UNIT_TMP" "$HOME/.config/systemd/user/jarvis-serve.service"
  systemctl --user daemon-reload
  systemctl --user enable --now jarvis-serve.service >/dev/null 2>&1 || true
  sleep 2
  if systemctl --user is-active --quiet jarvis-serve.service; then
    info "systemd service active (user)"
    installed=1
  else
    info "user unit installed but not active (user manager may not linger)"
  fi
fi

if [ "$installed" = "0" ]; then
  echo
  echo "install: WARNING: could not verify an active systemd service."
  echo "  Manual fallback: nohup python3 $ROOT/server/server.py --config $CONFIG >/dev/null 2>&1 &"
  echo "  Then verify: curl http://127.0.0.1:8765/healthz"
fi

# --- 5. smoke test -----------------------------------------------------------
sleep 1
PORT="$(python3 -c 'import json; print(json.load(open("'"$CONFIG"'"))["port"])')"
if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  info "healthz ok on 127.0.0.1:$PORT"
else
  BIND_NOW="$(python3 -c 'import json; print(json.load(open("'"$CONFIG"'"))["bind"])')"
  if curl -sf "http://$BIND_NOW:$PORT/healthz" >/dev/null 2>&1; then
    info "healthz ok on $BIND_NOW:$PORT"
  else
    fail "server not responding on $PORT; check $LOGS/server.log"
  fi
fi

echo
echo "=== server side done. Agent side remains (see agent/SKILL.md) ==="
echo "  1. copy hooks/queue-watch.sh -> ~/hooks/scripts/jarvis-serve-queue.sh"
echo "  2. register + dry-run + enable the hook via the hooks tool"
echo "  3. end-to-end curl test of /v1/chat/completions"
echo "  4. give the user: token (above), baseURL http://<bind>:$PORT/v1,"
echo "     model 'muse-agent', and opencode/opencode.json.example"
