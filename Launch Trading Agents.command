#!/bin/zsh
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

# Ensure the runner keeps running independently, then open the web console as an
# optional control surface.

runner_status=$(PYTHONPATH="$SCRIPT_DIR" "$PYTHON_BIN" - <<PY
from pathlib import Path
from trading_agents.config import load_settings
from trading_agents.service_manager import start_runner_service

settings = load_settings()
result = start_runner_service(settings, Path("$SCRIPT_DIR"))
print(result["status"])
PY
)

echo "Runner service: $runner_status"

existing_pid=$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null)
if [ -n "$existing_pid" ]; then
  kill "$existing_pid" 2>/dev/null
  sleep 1
fi

pkill -f "trading_agents_web.py" 2>/dev/null
sleep 1

"$PYTHON_BIN" "$SCRIPT_DIR/trading_agents_web.py" &
web_pid=$!

sleep 2
open "http://127.0.0.1:8765/" 2>/dev/null

caffeinate -im -w "$web_pid" &
caffeinate_pid=$!

wait "$web_pid"
kill "$caffeinate_pid" 2>/dev/null
