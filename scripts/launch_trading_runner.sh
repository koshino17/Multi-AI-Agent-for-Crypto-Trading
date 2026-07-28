#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

env_payload="$(
"$PYTHON_BIN" - <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys
from trading_agents.config import load_settings
from trading_agents.storage import build_storage_layout, mode_storage_root

required = ("TRADING_MODE", "SYMBOL", "OBSERVATION_POOL")
missing = [name for name in required if not os.getenv(name)]
if missing:
    mode = os.getenv("TRADING_MODE", "bybit-demo-perp")
    data_root = os.getenv("DATA_ROOT") or str(
        Path.home() / "Library" / "Application Support" / "TradePulse" / "state"
    )
    storage = build_storage_layout(str(mode_storage_root(data_root, mode)))
    detail = "Runtime .env missing required launch keys: " + ", ".join(missing)
    storage.runner_status.write_text(
        json.dumps(
            {
                "status": "blocked",
                "mode": mode,
                "symbol": os.getenv("SYMBOL", ""),
                "detail": detail,
                "reason_code": "missing_runtime_env",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stderr.write(
        "TradePulse launcher refused to start because runtime .env is missing: "
        + ", ".join(missing)
        + "\n"
    )
    sys.exit(78)

settings = load_settings()
storage = build_storage_layout(str(mode_storage_root(settings.data_root, settings.trading_mode)))
symbols = ",".join(settings.observation_pool) or settings.symbol
values = {
    "DATA_ROOT": settings.data_root,
    "MODE": settings.trading_mode,
    "SYMBOLS": symbols,
    "MONITOR_INTERVAL": settings.monitor_interval_seconds,
    "RUNNER_PID": str(storage.runner_pid),
    "NOTION_LOCK": str(storage.notion_sync_lock),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)" || exit $?

eval "$env_payload"

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

exec "$PYTHON_BIN" -m trading_agents.runner --mode "$MODE" --symbol "$SYMBOLS" --interval "$MONITOR_INTERVAL"
