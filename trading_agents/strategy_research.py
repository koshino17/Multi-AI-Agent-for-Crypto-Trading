from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_agents.alpha_arena import fetch_bybit_public_klines
from trading_agents.external_benchmarks import (
    _RULE_GENERATORS,
    _load_alpha_arena_normalized_signals,
    benchmark_signal_groups,
    build_benchmark_cost_model,
    load_external_benchmark_library,
    uses_custom_benchmark_cost_model,
)


def _candidate_sort_key(item: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(item.get("expectancy_pct", 0.0) or 0.0),
        float(item.get("profit_factor", 0.0) or 0.0),
        float(item.get("cumulative_return_pct", 0.0) or 0.0),
    )


def _preferred_live_cost_safe_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    safe_rows = [row for row in rows if not uses_custom_benchmark_cost_model(row)]
    return (safe_rows or rows or [{}])[0]


def _tournament_markdown(payload: dict[str, Any]) -> str:
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


def run_strategy_tournament(
    *,
    settings,
    storage,
    symbol: str,
    limit: int = 1000,
    include_alpha: bool = False,
) -> dict[str, Any]:
    baseline_id, library = load_external_benchmark_library(settings.external_benchmark_library_path)
    candles = fetch_bybit_public_klines(symbol, settings.timeframe, limit=max(120, min(int(limit), 1000)))
    if not candles:
        raise RuntimeError(f"No candles fetched for {symbol}.")

    default_cost_model = build_benchmark_cost_model(settings)
    ranked_rows: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, str]] = []

    for candidate in library:
        if candidate.kind == "alpha_arena_dataset":
            if not include_alpha:
                skipped_candidates.append({"candidate_id": candidate.id, "reason": "alpha candidate skipped without --include-alpha"})
                continue
            alpha_signals = _load_alpha_arena_normalized_signals(
                storage.alpha_arena_normalized,
                symbol=symbol,
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
        signals = generator(candles, symbol=symbol, candidate=candidate)
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
        ranked_rows.append(
            {
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
        )

    ranked_rows.sort(key=_candidate_sort_key, reverse=True)
    generated_at = datetime.now(timezone.utc)
    window_start = datetime.fromtimestamp(int(candles[0]["timestamp_ms"]) / 1000, tz=timezone.utc)
    window_end = datetime.fromtimestamp(int(candles[-1]["timestamp_ms"]) / 1000, tz=timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%S")
    output_dir = storage.benchmark_reports
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(),
        "symbol": symbol,
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
    json_path = output_dir / f"strategy-tournament-{symbol.replace('/', '-')}-{stamp}.json"
    md_path = output_dir / f"strategy-tournament-{symbol.replace('/', '-')}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    md_path.write_text(_tournament_markdown(payload))
    return {
        "payload": payload,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "top_candidate": _preferred_live_cost_safe_row(ranked_rows),
    }


def _aggregate_candidate_rows(
    *,
    research_runs: list[dict[str, Any]],
    focus_symbol: str,
    validation_symbols: tuple[str, ...],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    validation_set = {item.upper() for item in validation_symbols}
    for run in research_runs:
        symbol = str(run.get("symbol", "")).upper()
        ranked_rows = run.get("ranked_results") or []
        for row in ranked_rows:
            candidate_id = str(row.get("candidate_id", "")).strip()
            if not candidate_id:
                continue
            bucket = grouped.setdefault(
                candidate_id,
                {
                    "candidate_id": candidate_id,
                    "focus_expectancies": [],
                    "focus_profit_factors": [],
                    "focus_positive_windows": 0,
                    "focus_window_count": 0,
                    "validation_expectancies": [],
                    "validation_profit_factors": [],
                    "validation_window_count": 0,
                    "uses_custom_cost_model": False,
                },
            )
            bucket["uses_custom_cost_model"] = bool(bucket["uses_custom_cost_model"]) or uses_custom_benchmark_cost_model(row)
            expectancy = float(row.get("expectancy_pct", 0.0) or 0.0)
            pf = float(row.get("profit_factor", 0.0) or 0.0)
            if symbol == focus_symbol.upper():
                bucket["focus_expectancies"].append(expectancy)
                bucket["focus_profit_factors"].append(pf)
                bucket["focus_window_count"] += 1
                if expectancy > 0.0 and pf > 1.0:
                    bucket["focus_positive_windows"] += 1
            elif symbol in validation_set:
                bucket["validation_expectancies"].append(expectancy)
                bucket["validation_profit_factors"].append(pf)
                bucket["validation_window_count"] += 1

    aggregated: list[dict[str, Any]] = []
    for item in grouped.values():
        focus_expectancies = item["focus_expectancies"]
        focus_profit_factors = item["focus_profit_factors"]
        validation_expectancies = item["validation_expectancies"]
        validation_profit_factors = item["validation_profit_factors"]
        avg_focus_expectancy = sum(focus_expectancies) / len(focus_expectancies) if focus_expectancies else 0.0
        avg_focus_pf = sum(focus_profit_factors) / len(focus_profit_factors) if focus_profit_factors else 0.0
        avg_validation_expectancy = sum(validation_expectancies) / len(validation_expectancies) if validation_expectancies else 0.0
        avg_validation_pf = sum(validation_profit_factors) / len(validation_profit_factors) if validation_profit_factors else 0.0
        validation_guard_pass = (
            not validation_expectancies
            or (
                min(validation_expectancies) > -0.20
                and min(validation_profit_factors or [1.0]) > 0.60
            )
        )
        aggregated.append(
            {
                "candidate_id": item["candidate_id"],
                "focus_positive_windows": item["focus_positive_windows"],
                "focus_window_count": item["focus_window_count"],
                "avg_focus_expectancy_pct": avg_focus_expectancy,
                "avg_focus_profit_factor": avg_focus_pf,
                "avg_validation_expectancy_pct": avg_validation_expectancy,
                "avg_validation_profit_factor": avg_validation_pf,
                "validation_window_count": item["validation_window_count"],
                "validation_guard_pass": validation_guard_pass,
                "uses_custom_cost_model": bool(item["uses_custom_cost_model"]),
            }
        )
    aggregated.sort(
        key=lambda row: (
            0 if bool(row["uses_custom_cost_model"]) else 1,
            int(row["focus_positive_windows"]),
            float(row["avg_focus_expectancy_pct"]),
            float(row["avg_focus_profit_factor"]),
            1 if bool(row["validation_guard_pass"]) else 0,
        ),
        reverse=True,
    )
    return aggregated


def _research_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Strategy Research Cycle - {payload['focus_symbol']}",
        "",
        f"- Generated At: {payload['generated_at']}",
        f"- Focus Symbol: {payload['focus_symbol']}",
        f"- Validation Symbols: {', '.join(payload['validation_symbols']) if payload['validation_symbols'] else 'none'}",
        f"- Lookback Windows: {', '.join(str(item) for item in payload['limits'])} candles",
        "- Anti-overfit policy: optimize on the focus symbol, then require validation symbols to avoid catastrophic drift rather than to dominate ranking.",
        "",
        "## Per-Window Leaders",
        "",
    ]
    for run in payload["runs"]:
        top = run.get("top_candidate") or {}
        lines.append(
            f"- {run.get('symbol')} / {run.get('limit')} candles: "
            f"{top.get('candidate_id', 'n/a')} "
            f"(expectancy={float(top.get('expectancy_pct', 0.0)):+.2f}% | "
            f"profit_factor={float(top.get('profit_factor', 0.0)):.2f} | "
            f"trades={int(top.get('trade_count', 0))})"
        )
    lines.extend(
        [
            "",
            "## Aggregate Candidate Ranking",
            "",
            "| Rank | Candidate | Focus Positive Windows | Avg Focus Expectancy | Avg Focus PF | Validation Pass | Avg Validation Expectancy | Avg Validation PF |",
            "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: |",
        ]
    )
    for index, row in enumerate(payload["aggregate_ranking"], start=1):
        lines.append(
            f"| {index} | {row['candidate_id']} | "
            f"{int(row['focus_positive_windows'])}/{int(row['focus_window_count'])} | "
            f"{float(row['avg_focus_expectancy_pct']):+.2f}% | "
            f"{float(row['avg_focus_profit_factor']):.2f} | "
            f"{'yes' if bool(row['validation_guard_pass']) else 'no'} | "
            f"{float(row['avg_validation_expectancy_pct']):+.2f}% | "
            f"{float(row['avg_validation_profit_factor']):.2f} |"
        )
    recommendation = payload.get("recommendation") or {}
    lines.extend(["", "## Recommendation", ""])
    lines.append(f"- Candidate: {recommendation.get('candidate_id', 'n/a')}")
    lines.append(f"- Verdict: {recommendation.get('verdict', 'n/a')}")
    if recommendation.get("rationale"):
        lines.append(f"- Rationale: {recommendation.get('rationale')}")
    return "\n".join(lines) + "\n"


def run_strategy_research_cycle(
    *,
    settings,
    storage,
    focus_symbol: str,
    validation_symbols: tuple[str, ...],
    limits: tuple[int, ...],
    include_alpha: bool = False,
) -> dict[str, Any]:
    symbols = [focus_symbol] + [item for item in validation_symbols if item and item != focus_symbol]
    generated_at = datetime.now(timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%S")
    runs: list[dict[str, Any]] = []
    for symbol in symbols:
        for limit in limits:
            tournament = run_strategy_tournament(
                settings=settings,
                storage=storage,
                symbol=symbol,
                limit=limit,
                include_alpha=include_alpha,
            )
            runs.append(
                {
                    "symbol": symbol,
                    "limit": int(limit),
                    "json_path": tournament["json_path"],
                    "md_path": tournament["md_path"],
                    "top_candidate": tournament["top_candidate"],
                    "ranked_results": tournament["payload"]["ranked_results"],
                }
            )

    aggregate_ranking = _aggregate_candidate_rows(
        research_runs=runs,
        focus_symbol=focus_symbol,
        validation_symbols=validation_symbols,
    )
    top_candidate = aggregate_ranking[0] if aggregate_ranking else {}
    verdict = "research_only"
    rationale = "No candidate produced enough positive focus windows to justify stronger promotion."
    if top_candidate:
        focus_positive = int(top_candidate.get("focus_positive_windows", 0))
        focus_windows = int(top_candidate.get("focus_window_count", 0))
        avg_focus_expectancy = float(top_candidate.get("avg_focus_expectancy_pct", 0.0))
        avg_focus_pf = float(top_candidate.get("avg_focus_profit_factor", 0.0))
        validation_ok = bool(top_candidate.get("validation_guard_pass", False))
        uses_custom_cost_model = bool(top_candidate.get("uses_custom_cost_model", False))
        if uses_custom_cost_model:
            rationale = "Top candidate still relies on a research-only custom cost model, so keep it out of shadow/live promotion."
        elif focus_windows > 0 and focus_positive == focus_windows and avg_focus_expectancy > 0.0 and avg_focus_pf > 1.0 and validation_ok:
            verdict = "promotion_candidate"
            rationale = "Candidate stayed positive across all focus windows and cleared the validation guard."
        elif focus_positive > 0 and avg_focus_expectancy > 0.0:
            verdict = "shadow_candidate"
            rationale = "Candidate shows positive edge on at least one focus window, but evidence is not stable enough for promotion."

    payload = {
        "generated_at": generated_at.isoformat(),
        "focus_symbol": focus_symbol,
        "validation_symbols": list(validation_symbols),
        "limits": [int(item) for item in limits],
        "runs": runs,
        "aggregate_ranking": aggregate_ranking,
        "recommendation": {
            "candidate_id": top_candidate.get("candidate_id", "") if top_candidate else "",
            "verdict": verdict,
            "rationale": rationale,
        },
    }
    json_path = storage.benchmark_reports / f"strategy-research-{focus_symbol.replace('/', '-')}-{stamp}.json"
    md_path = storage.benchmark_reports / f"strategy-research-{focus_symbol.replace('/', '-')}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    md_path.write_text(_research_markdown(payload))
    state_path = storage.service / "strategy_research_latest.json"
    state_path.write_text(json.dumps({**payload, "json_path": str(json_path), "md_path": str(md_path)}, ensure_ascii=False, indent=2))
    return {
        "status": "updated",
        "generated_at": payload["generated_at"],
        "json_path": str(json_path),
        "md_path": str(md_path),
        "recommendation": payload["recommendation"],
    }
