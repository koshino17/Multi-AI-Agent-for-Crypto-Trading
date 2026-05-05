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
from trading_agents.strategy_research import run_strategy_research_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a multi-window strategy research cycle for TradePulse.")
    parser.add_argument("--focus-symbol", default="SOL/USDT", help="Primary research symbol, e.g. SOL/USDT")
    parser.add_argument("--validation-symbols", default="BTC/USDT,ETH/USDT", help="Comma-separated validation symbols")
    parser.add_argument("--limits", default="320,1000", help="Comma-separated candle windows for anti-overfit checks")
    parser.add_argument("--include-alpha", action="store_true", help="Include alpha-arena dataset candidates if available")
    args = parser.parse_args()

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    validation_symbols = tuple(item.strip() for item in args.validation_symbols.split(",") if item.strip())
    limits = tuple(int(item.strip()) for item in args.limits.split(",") if item.strip())
    result = run_strategy_research_cycle(
        settings=settings,
        storage=storage,
        focus_symbol=args.focus_symbol,
        validation_symbols=validation_symbols,
        limits=limits,
        include_alpha=args.include_alpha,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
