#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_agents.alpha_arena import fetch_bybit_public_klines
from trading_agents.config import load_settings
from trading_agents.external_benchmarks import (
    _load_alpha_arena_normalized_signals,
    build_benchmark_cost_model,
    _RULE_GENERATORS,
    benchmark_signal_groups,
    load_external_benchmark_library,
)
from trading_agents.storage import build_storage_layout


def _candidate_sort_key(item: dict) -> tuple[float, float, float]:
    return (
        float(item.get("expectancy_pct", 0.0) or 0.0),
        float(item.get("profit_factor", 0.0) or 0.0),
        float(item.get("cumulative_return_pct", 0.0) or 0.0),
    )


def _render_markdown(payload: dict) -> str:
    has_custom_costs = any(bool(row.get("uses_custom_cost_model")) for row in payload["ranked_results"])
    lines = [
        f"# Strategy Tournament - {payload['symbol']}",
        "",
        f"- Generated At: {payload['generated_at']}",
        f"- Timeframe: {payload['timeframe']}",
        f"- Candles: {payload['candle_count']}",
        f"- Window Start (UTC): {payload['window_start_utc']}",
        f"- Window End (UTC): {payload['window_end_utc']}",
        (
            f"- Cost Model: round-trip fee {payload['cost_model']['round_trip_fee_pct']:.2f}% | "
            f"round-trip slippage {payload['cost_model']['round_trip_slippage_pct']:.2f}% | "
            f"funding integrated={'yes' if payload['cost_model']['funding_integrated'] else 'no'}"
        ),
        (
            "- Candidate-specific cost overrides: "
            + ("present" if has_custom_costs else "none")
        ),
        "",
        "| Rank | Candidate | Expectancy | PF | Cumulative | Win Rate | Trades | Avg Return | Round-trip Cost |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(payload["ranked_results"], start=1):
        lines.append(
            f"| {index} | {row['candidate_id']} | "
            f"{float(row['expectancy_pct']):+.2f}% | "
            f"{float(row['profit_factor']):.2f} | "
            f"{float(row['cumulative_return_pct']):+.2f}% | "
            f"{float(row['win_rate'])*100.0:.1f}% | "
            f"{int(row['trade_count'])} | "
            f"{float(row['avg_return_pct']):+.2f}% | "
            f"{float(row.get('total_round_trip_cost_pct', 0.0)):.2f}% |"
        )
    skipped = payload.get("skipped_candidates") or []
    if skipped:
        lines.extend(["", "## Skipped Candidates", ""])
        for item in skipped:
            lines.append(f"- `{item.get('candidate_id', 'unknown')}`: {item.get('reason', 'unknown')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a cost-aware strategy tournament across TradePulse benchmark candidates.")
    parser.add_argument("--symbol", default="SOL/USDT", help="Benchmark symbol, e.g. SOL/USDT")
    parser.add_argument("--limit", type=int, default=1000, help="Number of candles to fetch (max 1000)")
    parser.add_argument("--include-alpha", action="store_true", help="Include alpha-arena dataset candidates if available")
    args = parser.parse_args()

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    baseline_id, library = load_external_benchmark_library(settings.external_benchmark_library_path)
    candles = fetch_bybit_public_klines(args.symbol, settings.timeframe, limit=max(120, min(args.limit, 1000)))
    if not candles:
        raise SystemExit("No candles fetched.")

    default_cost_model = build_benchmark_cost_model(settings)

    ranked_rows: list[dict] = []
    skipped_candidates: list[dict[str, str]] = []
    for candidate in library:
        if candidate.kind == "alpha_arena_dataset":
            if not args.include_alpha:
                skipped_candidates.append({"candidate_id": candidate.id, "reason": "alpha candidate skipped without --include-alpha"})
                continue
            alpha_signals = _load_alpha_arena_normalized_signals(
                storage.alpha_arena_normalized,
                symbol=args.symbol,
                max_signals=min(
                    max(int(candidate.params.get("max_signals", settings.external_benchmark_max_alpha_signals)), 1),
                    settings.external_benchmark_max_alpha_signals,
                ),
            )
            if not alpha_signals:
                skipped_candidates.append({"candidate_id": candidate.id, "reason": "no normalized alpha signals found for symbol"})
                continue
            grouped_results = benchmark_signal_groups(
                candles,
                {f"alpha_arena::{model}": signal_group for model, signal_group in alpha_signals.items()},
                hold_bars=candidate.hold_bars,
                take_profit_pct=candidate.take_profit_pct,
                stop_loss_pct=candidate.stop_loss_pct,
                candidate=None,
                cost_model=default_cost_model,
            )
            for key, signal_group in alpha_signals.items():
                result = grouped_results.get(f"alpha_arena::{key}")
                if result is None:
                    skipped_candidates.append({"candidate_id": f"alpha_arena::{key}", "reason": "benchmark returned no result"})
                    continue
                ranked_rows.append(
                    {
                        "candidate_id": f"alpha_arena::{key}",
                        "candidate_name": f"{candidate.name} / {key}",
                        "source": candidate.source,
                        "baseline": False,
                        "signal_count": len(signal_group),
                        "round_trip_fee_pct": default_cost_model.round_trip_fee_pct * 100.0,
                        "round_trip_slippage_pct": default_cost_model.round_trip_slippage_pct * 100.0,
                        "funding_fee_pct": default_cost_model.funding_fee_pct * 100.0,
                        "total_round_trip_cost_pct": default_cost_model.total_round_trip_cost_pct * 100.0,
                        "uses_custom_cost_model": False,
                        **asdict(result),
                    }
                )
            continue
        generator = _RULE_GENERATORS.get(candidate.generator) or _RULE_GENERATORS.get(candidate.kind) or _RULE_GENERATORS.get(candidate.id)
        if generator is None:
            skipped_candidates.append({"candidate_id": candidate.id, "reason": f"no generator registered for {candidate.generator or candidate.kind or candidate.id}"})
            continue
        signals = generator(candles, symbol=args.symbol, candidate=candidate)
        candidate_cost_model = build_benchmark_cost_model(settings, candidate)
        result = benchmark_signal_groups(
            candles,
            {candidate.id: signals},
            hold_bars=candidate.hold_bars,
            take_profit_pct=candidate.take_profit_pct,
            stop_loss_pct=candidate.stop_loss_pct,
            candidate=candidate,
            cost_model=candidate_cost_model,
        ).get(candidate.id)
        if result is None:
            skipped_candidates.append({"candidate_id": candidate.id, "reason": "benchmark returned no result"})
            continue
        row = {
            "candidate_id": candidate.id,
            "candidate_name": candidate.name,
            "source": candidate.source,
            "baseline": candidate.id == baseline_id,
            "signal_count": len(signals),
            "round_trip_fee_pct": candidate_cost_model.round_trip_fee_pct * 100.0,
            "round_trip_slippage_pct": candidate_cost_model.round_trip_slippage_pct * 100.0,
            "funding_fee_pct": candidate_cost_model.funding_fee_pct * 100.0,
            "total_round_trip_cost_pct": candidate_cost_model.total_round_trip_cost_pct * 100.0,
            "uses_custom_cost_model": candidate_cost_model != default_cost_model,
            **asdict(result),
        }
        ranked_rows.append(row)

    ranked_rows.sort(key=_candidate_sort_key, reverse=True)
    generated_at = datetime.now(timezone.utc)
    window_start = datetime.fromtimestamp(int(candles[0]["timestamp_ms"]) / 1000, tz=timezone.utc)
    window_end = datetime.fromtimestamp(int(candles[-1]["timestamp_ms"]) / 1000, tz=timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%S")
    output_dir = storage.benchmark_reports
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(),
        "symbol": args.symbol,
        "timeframe": settings.timeframe,
        "baseline_strategy_id": baseline_id,
        "candle_count": len(candles),
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "cost_model": {
            "round_trip_fee_pct": default_cost_model.round_trip_fee_pct * 100.0,
            "round_trip_slippage_pct": default_cost_model.round_trip_slippage_pct * 100.0,
            "funding_fee_pct": default_cost_model.funding_fee_pct * 100.0,
            "funding_integrated": False,
        },
        "ranked_results": ranked_rows,
        "skipped_candidates": skipped_candidates,
    }
    json_path = output_dir / f"strategy-tournament-{args.symbol.replace('/', '-')}-{stamp}.json"
    md_path = output_dir / f"strategy-tournament-{args.symbol.replace('/', '-')}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    md_path.write_text(_render_markdown(payload))
    print(json.dumps({"json_path": str(json_path), "md_path": str(md_path), "top_candidate": ranked_rows[0] if ranked_rows else {}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
