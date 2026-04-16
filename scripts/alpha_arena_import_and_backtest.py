#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_agents.alpha_arena import (
    backtest_alpha_arena_signals,
    fetch_bybit_public_klines,
    load_alpha_arena_signals,
    save_backtest_report,
    write_normalized_signals,
)
from trading_agents.config import load_settings
from trading_agents.storage import build_storage_layout


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Alpha Arena public signals and run a basic benchmark backtest.")
    parser.add_argument("--input", required=True, help="Path to a JSON export containing public Alpha Arena-like signals.")
    parser.add_argument("--symbol", default="BTC/USDT", help="Default symbol if input records do not provide one.")
    parser.add_argument("--model", default="alpha_arena_public", help="Default model label if input records do not provide one.")
    parser.add_argument("--source-url", default="", help="Optional original source URL for traceability.")
    parser.add_argument("--timeframe", default="15m", help="Benchmark timeframe for public candles.")
    parser.add_argument("--hold-bars", type=int, default=4, help="How many bars to hold each imported signal during benchmark replay.")
    parser.add_argument("--take-profit-pct", type=float, default=0.9, help="Take-profit percentage used for benchmark replay.")
    parser.add_argument("--stop-loss-pct", type=float, default=0.45, help="Stop-loss percentage used for benchmark replay.")
    args = parser.parse_args()

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    signals = load_alpha_arena_signals(
        args.input,
        default_symbol=args.symbol,
        default_model=args.model,
        source_url=args.source_url,
    )
    if not signals:
        raise SystemExit("No valid Alpha Arena signals found in the input JSON.")

    timestamp_label = datetime.now().strftime("%Y%m%dT%H%M%S")
    normalized_path = storage.alpha_arena_normalized / f"alpha-arena-signals-{timestamp_label}.jsonl"
    write_normalized_signals(signals, str(normalized_path))

    candles = fetch_bybit_public_klines(args.symbol, args.timeframe, limit=max(300, args.hold_bars * 40))
    results = backtest_alpha_arena_signals(
        signals,
        candles,
        hold_bars=args.hold_bars,
        take_profit_pct=args.take_profit_pct / 100.0,
        stop_loss_pct=args.stop_loss_pct / 100.0,
    )
    report_path = storage.reports / f"alpha-arena-benchmark-{timestamp_label}.json"
    save_backtest_report(results, str(report_path))

    overall = results.get("__overall__")
    print(
        {
            "signals_loaded": len(signals),
            "normalized_path": str(normalized_path),
            "report_path": str(report_path),
            "overall": overall.__dict__ if overall is not None else {},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
