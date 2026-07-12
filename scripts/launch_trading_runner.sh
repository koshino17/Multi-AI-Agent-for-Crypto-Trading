#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

LAUNCHER_VALUES="$(
"$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
import shlex
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


runtime_root = Path.cwd()
runtime_env = runtime_root / ".env"
if load_dotenv is not None:
    load_dotenv(runtime_env, override=False)


def required_missing() -> list[str]:
    required = ["TRADING_MODE", "SYMBOL", "OBSERVATION_POOL"]
    if os.getenv("TRADING_MODE", "").strip().lower() == "bybit-demo-perp":
        required.extend(["BYBIT_DEMO_API_KEY", "BYBIT_DEMO_SECRET"])
    return [name for name in required if not os.getenv(name)]


missing = required_missing()
if missing:
    project_root_hint = runtime_root / ".project_root"
    try:
        project_root = Path(project_root_hint.read_text().strip()).expanduser()
    except OSError:
        project_root = None
    project_env = project_root / ".env" if project_root else None
    if project_env and project_env.exists() and load_dotenv is not None:
        runtime_data_root = os.getenv("DATA_ROOT", "").strip()
        env_lines = project_env.read_text().splitlines()
        if runtime_data_root:
            data_root_written = False
            for index, line in enumerate(env_lines):
                if line.startswith("DATA_ROOT="):
                    env_lines[index] = f"DATA_ROOT={runtime_data_root}"
                    data_root_written = True
                    break
            if not data_root_written:
                env_lines.append(f"DATA_ROOT={runtime_data_root}")
        runtime_env.write_text("\n".join(env_lines) + ("\n" if env_lines else ""))
        load_dotenv(runtime_env, override=True)
        missing = required_missing()

if missing:
    sys.stderr.write(
        "TradePulse launcher refused to start because runtime .env is missing: "
        + ", ".join(missing)
        + "\n"
    )
    sys.exit(78)

from trading_agents.config import load_settings
from trading_agents.storage import build_storage_layout, mode_storage_root

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
)"
launcher_status=$?
if [ "$launcher_status" -ne 0 ]; then
  exit "$launcher_status"
fi
eval "$LAUNCHER_VALUES"

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
