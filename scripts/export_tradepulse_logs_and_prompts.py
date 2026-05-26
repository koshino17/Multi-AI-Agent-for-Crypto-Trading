#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_agents.config import load_settings
from trading_agents.service_manager import runner_state_root
from trading_agents.storage import build_storage_layout, mode_storage_root


DEFAULT_EXPORT_ROOT = Path.home() / "Desktop" / "TradePulse_Exports"
LAUNCHD_LOG_PATH = Path.home() / "Library" / "Logs" / "TradePulse" / "launchd-runner.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export TradePulse logs and prompt traces into a single desktop folder."
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_EXPORT_ROOT),
        help="Folder that will contain timestamped exports.",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Optional export folder name. Defaults to logs_and_prompts_<timestamp>.",
    )
    parser.add_argument(
        "--mode",
        default="",
        help="Trading mode to export. Defaults to the current configured mode.",
    )
    parser.add_argument(
        "--date-label",
        default="latest",
        help="Agent trace date label to export, or 'latest' to auto-select the newest one.",
    )
    parser.add_argument(
        "--include-all-agent-traces",
        action="store_true",
        help="Copy every available agent trace date instead of only one date folder.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=0,
        help="Export only the last N calendar days, including today.",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Optional inclusive start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Optional inclusive end date in YYYY-MM-DD format.",
    )
    return parser.parse_args()


def resolve_storage(mode: str):
    state_root = runner_state_root()
    storage_root = mode_storage_root(state_root, mode)
    return build_storage_layout(str(storage_root))


def latest_trace_date(agent_trace_root: Path) -> str | None:
    candidates = sorted(path.name for path in agent_trace_root.iterdir() if path.is_dir())
    if not candidates:
        return None
    return candidates[-1]


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def resolve_date_range(args: argparse.Namespace) -> tuple[date | None, date | None]:
    start_date = parse_iso_date(args.start_date) if args.start_date.strip() else None
    end_date = parse_iso_date(args.end_date) if args.end_date.strip() else None

    if args.since_days > 0:
        today = datetime.now().astimezone().date()
        end_date = end_date or today
        start_date = start_date or (end_date - timedelta(days=args.since_days - 1))

    if start_date and end_date and start_date > end_date:
        raise ValueError("start date must be on or before end date")
    return start_date, end_date


def in_date_range(value: date, start_date: date | None, end_date: date | None) -> bool:
    if start_date and value < start_date:
        return False
    if end_date and value > end_date:
        return False
    return True


def trace_dirs_for_range(agent_trace_root: Path, start_date: date | None, end_date: date | None) -> list[Path]:
    selected: list[Path] = []
    for path in sorted(candidate for candidate in agent_trace_root.iterdir() if candidate.is_dir()):
        try:
            trace_date = parse_iso_date(path.name)
        except ValueError:
            continue
        if in_date_range(trace_date, start_date, end_date):
            selected.append(path)
    return selected


def copy_if_exists(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
    return True


def filter_log_by_date_range(source: Path, target: Path, start_date: date | None, end_date: date | None) -> bool:
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)

    kept_lines: list[str] = []
    current_block_in_range = False
    for raw_line in source.read_text(errors="replace").splitlines():
        if raw_line.startswith("{"):
            current_block_in_range = False
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            timestamp = str(payload.get("timestamp", "")).strip()
            if not timestamp:
                continue
            try:
                line_date = datetime.fromisoformat(timestamp).date()
            except ValueError:
                continue
            if in_date_range(line_date, start_date, end_date):
                kept_lines.append(raw_line)
                current_block_in_range = True
            continue

        if current_block_in_range:
            kept_lines.append(raw_line)

    target.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""))
    return True


def main() -> int:
    args = parse_args()
    settings = load_settings()
    mode = args.mode.strip() or settings.trading_mode
    storage = resolve_storage(mode)
    start_date, end_date = resolve_date_range(args)

    output_root = Path(args.output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.name.strip():
        export_name = args.name.strip()
    elif start_date or end_date:
        start_label = (start_date or end_date).isoformat() if (start_date or end_date) else "unknown"
        end_label = (end_date or start_date).isoformat() if (end_date or start_date) else "unknown"
        export_name = f"logs_and_prompts_{start_label}_to_{end_label}"
    else:
        export_name = f"logs_and_prompts_{timestamp}"
    export_root = output_root / export_name
    export_root.mkdir(parents=True, exist_ok=True)

    copied: dict[str, list[str]] = {
        "logs": [],
        "prompts": [],
    }

    if filter_log_by_date_range(storage.runner_log, export_root / "logs" / "runner.log", start_date, end_date):
        copied["logs"].append(str(storage.runner_log))
    if filter_log_by_date_range(LAUNCHD_LOG_PATH, export_root / "logs" / "launchd-runner.log", start_date, end_date):
        copied["logs"].append(str(LAUNCHD_LOG_PATH))

    if start_date or end_date:
        for trace_dir in trace_dirs_for_range(storage.agent_traces, start_date, end_date):
            if copy_if_exists(trace_dir, export_root / "prompts" / trace_dir.name):
                copied["prompts"].append(str(trace_dir))
    elif args.include_all_agent_traces:
        for trace_dir in sorted(path for path in storage.agent_traces.iterdir() if path.is_dir()):
            if copy_if_exists(trace_dir, export_root / "prompts" / trace_dir.name):
                copied["prompts"].append(str(trace_dir))
    else:
        date_label = args.date_label.strip() or "latest"
        if date_label == "latest":
            date_label = latest_trace_date(storage.agent_traces) or ""
        if date_label:
            trace_dir = storage.agent_traces / date_label
            if copy_if_exists(trace_dir, export_root / "prompts" / trace_dir.name):
                copied["prompts"].append(str(trace_dir))

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": mode,
        "export_root": str(export_root),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "copied": copied,
    }
    (export_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(str(export_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
