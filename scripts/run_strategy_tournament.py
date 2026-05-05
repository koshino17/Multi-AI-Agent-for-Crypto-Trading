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
from trading_agents.storage import build_storage_layout
from trading_agents.strategy_research import run_strategy_tournament


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a cost-aware strategy tournament across TradePulse benchmark candidates.")
    parser.add_argument("--symbol", default="SOL/USDT", help="Benchmark symbol, e.g. SOL/USDT")
    parser.add_argument("--limit", type=int, default=1000, help="Number of candles to fetch (max 1000)")
    parser.add_argument("--include-alpha", action="store_true", help="Include alpha-arena dataset candidates if available")
    args = parser.parse_args()

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    result = run_strategy_tournament(
        settings=settings,
        storage=storage,
        symbol=args.symbol,
        limit=args.limit,
        include_alpha=args.include_alpha,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
