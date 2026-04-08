#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

eval "$(
python3 - <<'PY'
import shlex
from trading_agents.config import load_settings
from trading_agents.storage import build_storage_layout
settings = load_settings()
storage = build_storage_layout(settings.data_root)
symbols = ",".join(settings.observation_pool) or settings.symbol
values = {
    "DATA_ROOT": settings.data_root,
    "MODE": settings.trading_mode,
    "SYMBOLS": symbols,
    "MONITOR_INTERVAL": settings.monitor_interval_seconds,
    "RUNNER_PID": str(storage.runner_pid),
    "RUNNER_LOG": str(storage.runner_log),
    "NOTION_LOCK": str(storage.notion_sync_lock),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)"

mkdir -p "$DATA_ROOT/service"

if [ -f "$RUNNER_PID" ]; then
  existing_pid=$(cat "$RUNNER_PID" 2>/dev/null || true)
  if [ -n "$existing_pid" ] && ! kill -0 "$existing_pid" 2>/dev/null; then
    rm -f "$RUNNER_PID"
  fi
fi

if [ -f "$NOTION_LOCK" ]; then
  now_epoch=$(date +%s)
  lock_epoch=$(stat -f %m "$NOTION_LOCK" 2>/dev/null || echo 0)
  if [ $((now_epoch - lock_epoch)) -gt 180 ]; then
    rm -f "$NOTION_LOCK"
  fi
fi

exec >> "$RUNNER_LOG" 2>&1
exec python3 -m trading_agents.runner --mode "$MODE" --symbol "$SYMBOLS" --interval "$MONITOR_INTERVAL"
