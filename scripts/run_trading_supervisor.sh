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
    "SUPERVISOR_PID": str(storage.runner_supervisor_pid),
    "RUNNER_PID": str(storage.runner_pid),
    "RUNNER_LOG": str(storage.runner_log),
    "SUPERVISOR_LOG": str(storage.runner_supervisor_log),
    "NOTION_LOCK": str(storage.notion_sync_lock),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)"

mkdir -p "$DATA_ROOT/service"
echo $$ > "$SUPERVISOR_PID"
exec >> "$SUPERVISOR_LOG" 2>&1

child_pid=""
stop_requested=0

cleanup() {
  rm -f "$SUPERVISOR_PID"
}

request_stop() {
  stop_requested=1
  if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
  fi
}

trap request_stop INT TERM
trap cleanup EXIT

while true; do
  if [ -f "$RUNNER_PID" ]; then
    existing_runner=$(cat "$RUNNER_PID" 2>/dev/null || true)
    if [ -n "$existing_runner" ] && ! kill -0 "$existing_runner" 2>/dev/null; then
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

  echo "$(date '+%F %T %Z') supervisor starting runner"
  python3 -m trading_agents.runner --mode "$MODE" --symbol "$SYMBOLS" --interval "$MONITOR_INTERVAL" >> "$RUNNER_LOG" 2>&1 &
  child_pid=$!
  wait "$child_pid"
  exit_code=$?
  child_pid=""

  if [ "$stop_requested" -eq 1 ]; then
    break
  fi

  echo "$(date '+%F %T %Z') runner exited with code $exit_code; restarting in 5s"
  sleep 5
done
