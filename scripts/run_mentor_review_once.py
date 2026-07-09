#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_agents.config import load_settings
from trading_agents.mentor_review import run_mentor_cycle
from trading_agents.reporting import completed_report_date_label, load_daily_summary_data
from trading_agents.storage import build_storage_layout, mode_storage_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one TradePulse mentor review cycle.")
    parser.add_argument("-date-label", "--date-label", default=completed_report_date_label())
    parser.add_argument("-mode", "--mode", default="")
    parser.add_argument("-no-promote", "--no-promote", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    mode = args.mode or settings.trading_mode
    storage = build_storage_layout(str(mode_storage_root(settings.data_root, mode)))
    daily_summary = load_daily_summary_data(
        storage.trade_logs,
        args.date_label,
        storage.runner_log,
        trading_mode=mode,
        storage_root=storage.root,
    )
    review_path = storage.service / f"daily_strategy_review-{args.date_label}.json"
    try:
        daily_review = json.loads(review_path.read_text())
    except Exception:
        daily_review = daily_summary.get("daily_strategy_review") or {}
    result = run_mentor_cycle(
        date_label=args.date_label,
        daily_summary=daily_summary,
        daily_review=daily_review,
        settings=settings,
        storage=storage,
        mode=mode,
        promote=not args.no_promote,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
