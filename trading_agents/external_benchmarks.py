from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

from trading_agents.alpha_arena import (
    AlphaArenaBacktestResult,
    AlphaArenaSignal,
    fetch_bybit_public_klines,
    write_normalized_signals,
)
from trading_agents.backtest import compute_adx, donchian_adx_signal


@dataclass(frozen=True)
class ExternalBenchmarkCandidate:
    id: str
    name: str
    kind: str
    generator: str
    source: str
    description: str
    hold_bars: int
    take_profit_pct: float
    stop_loss_pct: float
    params: dict[str, Any]


@dataclass(frozen=True)
class ExternalBenchmarkResult:
    candidate_id: str
    candidate_name: str
    source: str
    symbol: str
    timeframe: str
    signal_count: int
    trade_count: int
    win_rate: float
    avg_return_pct: float
    cumulative_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float
    profit_factor: float
    round_trip_fee_pct: float = 0.0
    round_trip_slippage_pct: float = 0.0
    funding_fee_pct: float = 0.0
    total_round_trip_cost_pct: float = 0.0
    uses_custom_cost_model: bool = False


@dataclass(frozen=True)
class BenchmarkCostModel:
    round_trip_fee_pct: float = 0.0
    round_trip_slippage_pct: float = 0.0
    funding_fee_pct: float = 0.0

    @property
    def total_round_trip_cost_pct(self) -> float:
        return max(self.round_trip_fee_pct, 0.0) + max(self.round_trip_slippage_pct, 0.0) + max(self.funding_fee_pct, 0.0)


def build_benchmark_cost_model(settings, candidate: ExternalBenchmarkCandidate | None = None) -> BenchmarkCostModel:
    taker_fee_pct = min(max(float(getattr(settings, "taker_fee_pct", 0.0) or 0.0), 0.0), 0.01)
    slippage_pct = min(max(float(getattr(settings, "external_benchmark_slippage_pct", 0.0) or 0.0), 0.0), 0.01)
    fee_round_trip = taker_fee_pct * 2.0
    slippage_round_trip = slippage_pct * 2.0
    funding_round_trip = 0.0
    if candidate is not None and isinstance(candidate.params, dict):
        if "assumed_round_trip_fee_pct" in candidate.params:
            fee_round_trip = max(float(candidate.params.get("assumed_round_trip_fee_pct", 0.0) or 0.0), 0.0) / 100.0
        if "assumed_round_trip_slippage_pct" in candidate.params:
            slippage_round_trip = max(float(candidate.params.get("assumed_round_trip_slippage_pct", 0.0) or 0.0), 0.0) / 100.0
        if "assumed_funding_fee_pct" in candidate.params:
            funding_round_trip = max(float(candidate.params.get("assumed_funding_fee_pct", 0.0) or 0.0), 0.0) / 100.0
    return BenchmarkCostModel(
        round_trip_fee_pct=fee_round_trip,
        round_trip_slippage_pct=slippage_round_trip,
        funding_fee_pct=funding_round_trip,
    )


def load_external_benchmark_library(path: str | Path) -> tuple[str, list[ExternalBenchmarkCandidate]]:
    payload = json.loads(Path(path).read_text())
    baseline = str(payload.get("baseline_strategy_id", "")).strip()
    candidates: list[ExternalBenchmarkCandidate] = []
    for item in payload.get("strategies", []):
        if not isinstance(item, dict):
            continue
        candidates.append(
            ExternalBenchmarkCandidate(
                id=str(item.get("id", "")).strip(),
                name=str(item.get("name", "")).strip(),
                kind=str(item.get("kind", "")).strip(),
                generator=str(item.get("generator", "")).strip(),
                source=str(item.get("source", "")).strip(),
                description=str(item.get("description", "")).strip(),
                hold_bars=max(int(item.get("hold_bars", 4) or 4), 1),
                take_profit_pct=float(item.get("take_profit_pct", 0.0) or 0.0) / 100.0,
                stop_loss_pct=float(item.get("stop_loss_pct", 0.0) or 0.0) / 100.0,
                params=item.get("params", {}) if isinstance(item.get("params"), dict) else {},
            )
        )
    return baseline, candidates


def load_external_benchmark_summary(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"status": "empty"}
    try:
        payload = json.loads(state_path.read_text())
    except Exception:
        return {"status": "error", "reason": "unable to read benchmark state"}
    return payload if isinstance(payload, dict) else {"status": "error", "reason": "invalid benchmark state"}


def _live_cost_only(results: list[ExternalBenchmarkResult]) -> list[ExternalBenchmarkResult]:
    return [item for item in results if not bool(item.uses_custom_cost_model)]


def refresh_external_benchmark_suite(
    *,
    storage,
    settings,
    symbols: list[str],
    force: bool = False,
) -> dict[str, Any]:
    state_path = storage.external_benchmark_state
    if not settings.external_benchmark_enabled:
        return {"status": "disabled", "reason": "external benchmark disabled"}
    existing = load_external_benchmark_summary(state_path)
    generated_at_raw = str(existing.get("generated_at", "")).strip()
    if generated_at_raw and not force:
        try:
            generated_at = datetime.fromisoformat(generated_at_raw)
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - generated_at < timedelta(hours=max(settings.external_benchmark_refresh_hours, 0.1)):
                return {
                    "status": "skipped",
                    "reason": "benchmark refresh interval not reached",
                    "generated_at": generated_at.isoformat(),
                    "report_path": existing.get("report_path", ""),
                }
        except Exception:
            pass

    baseline_strategy_id, library = load_external_benchmark_library(settings.external_benchmark_library_path)
    benchmark_symbols = list(dict.fromkeys([*symbols, *_discover_alpha_symbols(storage.alpha_arena_normalized)]))
    normalized_outputs: list[str] = []
    symbol_results: dict[str, list[dict[str, Any]]] = {}
    all_results: list[ExternalBenchmarkResult] = []
    generated_at = datetime.now(timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%S")
    default_cost_model = build_benchmark_cost_model(settings)

    for symbol in benchmark_symbols:
        candles = fetch_bybit_public_klines(symbol, settings.timeframe, limit=max(settings.external_benchmark_limit, 120))
        results_for_symbol: list[ExternalBenchmarkResult] = []
        for candidate in library:
            if candidate.kind == "alpha_arena_dataset":
                alpha_signals = _load_alpha_arena_normalized_signals(
                    storage.alpha_arena_normalized,
                    symbol=symbol,
                    max_signals=min(
                        max(int(candidate.params.get("max_signals", settings.external_benchmark_max_alpha_signals)), 1),
                        settings.external_benchmark_max_alpha_signals,
                    ),
                )
                if not alpha_signals:
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
                for key, result in grouped_results.items():
                    model_name = key.split("::", 1)[-1]
                    results_for_symbol.append(
                        ExternalBenchmarkResult(
                            candidate_id=key,
                            candidate_name=f"{candidate.name} / {model_name}",
                            source=candidate.source,
                            symbol=symbol,
                            timeframe=settings.timeframe,
                            signal_count=sum(len(group) for group in alpha_signals.values()),
                            trade_count=result.trade_count,
                            win_rate=result.win_rate,
                            avg_return_pct=result.avg_return_pct,
                            cumulative_return_pct=result.cumulative_return_pct,
                            avg_win_pct=result.avg_win_pct,
                            avg_loss_pct=result.avg_loss_pct,
                            expectancy_pct=result.expectancy_pct,
                            profit_factor=result.profit_factor,
                            round_trip_fee_pct=default_cost_model.round_trip_fee_pct * 100.0,
                            round_trip_slippage_pct=default_cost_model.round_trip_slippage_pct * 100.0,
                            funding_fee_pct=default_cost_model.funding_fee_pct * 100.0,
                            total_round_trip_cost_pct=default_cost_model.total_round_trip_cost_pct * 100.0,
                            uses_custom_cost_model=False,
                        )
                    )
                continue

            generator = _RULE_GENERATORS.get(candidate.generator) or _RULE_GENERATORS.get(candidate.kind) or _RULE_GENERATORS.get(candidate.id)
            if generator is None:
                generator = _RULE_GENERATORS.get({
                    "donchian_adx_perp_v1": "donchian_adx",
                    "grid_range_reversion_v1": "grid_range_reversion",
                    "bollinger_rsi_mean_reversion_v1": "bollinger_rsi_mean_reversion",
                }.get(candidate.id, ""))
            if generator is None:
                continue
            signals = generator(candles, symbol=symbol, candidate=candidate)
            if signals:
                output_path = storage.external_benchmark_normalized / f"{candidate.id}-{symbol.replace('/', '-')}-{stamp}.jsonl"
                write_normalized_signals(signals, str(output_path))
                normalized_outputs.append(str(output_path))
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
                continue
            results_for_symbol.append(
                ExternalBenchmarkResult(
                    candidate_id=candidate.id,
                    candidate_name=candidate.name,
                    source=candidate.source,
                    symbol=symbol,
                    timeframe=settings.timeframe,
                    signal_count=len(signals),
                    trade_count=result.trade_count,
                    win_rate=result.win_rate,
                    avg_return_pct=result.avg_return_pct,
                    cumulative_return_pct=result.cumulative_return_pct,
                    avg_win_pct=result.avg_win_pct,
                    avg_loss_pct=result.avg_loss_pct,
                    expectancy_pct=result.expectancy_pct,
                    profit_factor=result.profit_factor,
                    round_trip_fee_pct=candidate_cost_model.round_trip_fee_pct * 100.0,
                    round_trip_slippage_pct=candidate_cost_model.round_trip_slippage_pct * 100.0,
                    funding_fee_pct=candidate_cost_model.funding_fee_pct * 100.0,
                    total_round_trip_cost_pct=candidate_cost_model.total_round_trip_cost_pct * 100.0,
                    uses_custom_cost_model=candidate_cost_model != default_cost_model,
                )
            )
        results_for_symbol.sort(key=_benchmark_sort_key, reverse=True)
        all_results.extend(results_for_symbol)
        symbol_results[symbol] = [asdict(item) for item in results_for_symbol]

    comparable_results = _live_cost_only(all_results)
    top_candidates = sorted(all_results, key=_benchmark_sort_key, reverse=True)[:8]
    top_candidates_live_cost = sorted(comparable_results, key=_benchmark_sort_key, reverse=True)[:8]
    top_alpha = [item for item in top_candidates if item.candidate_id.startswith("alpha_arena::")]
    top_alpha_live_cost = [item for item in top_candidates_live_cost if item.candidate_id.startswith("alpha_arena::")]
    top_by_symbol = {
        symbol: results[0]
        for symbol, results in (
            (key, [ExternalBenchmarkResult(**item) for item in value])
            for key, value in symbol_results.items()
        )
        if results
    }
    top_by_symbol_live_cost = {
        symbol: results[0]
        for symbol, results in (
            (key, _live_cost_only([ExternalBenchmarkResult(**item) for item in value]))
            for key, value in symbol_results.items()
        )
        if results
    }
    snapshot = {
        "status": "updated",
        "generated_at": generated_at.isoformat(),
        "timeframe": settings.timeframe,
        "baseline_strategy_id": baseline_strategy_id,
        "cost_model": {
            "round_trip_fee_pct": default_cost_model.round_trip_fee_pct * 100.0,
            "round_trip_slippage_pct": default_cost_model.round_trip_slippage_pct * 100.0,
            "funding_fee_pct": default_cost_model.funding_fee_pct * 100.0,
            "total_round_trip_cost_pct": default_cost_model.total_round_trip_cost_pct * 100.0,
            "funding_integrated": False,
        },
        "symbols": benchmark_symbols,
        "normalized_outputs": normalized_outputs[-24:],
        "report_path": "",
        "results": symbol_results,
        "top_candidates": [asdict(item) for item in top_candidates],
        "top_candidates_live_cost": [asdict(item) for item in top_candidates_live_cost],
        "top_alpha_arena_candidates": [asdict(item) for item in top_alpha],
        "top_alpha_arena_candidates_live_cost": [asdict(item) for item in top_alpha_live_cost],
        "top_by_symbol": {key: asdict(value) for key, value in top_by_symbol.items()},
        "top_by_symbol_live_cost": {key: asdict(value) for key, value in top_by_symbol_live_cost.items()},
    }
    report_path = storage.benchmark_reports / f"external-benchmark-{stamp}.json"
    report_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    snapshot["report_path"] = str(report_path)
    state_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return {
        "status": "updated",
        "generated_at": snapshot["generated_at"],
        "report_path": str(report_path),
        "candidate_count": len(all_results),
        "top_candidate": asdict(top_candidates[0]) if top_candidates else {},
        "top_alpha_candidate": asdict(top_alpha[0]) if top_alpha else {},
    }


def benchmark_signal_groups(
    candles: list[dict[str, float | int]],
    signal_groups: dict[str, list[AlphaArenaSignal]],
    *,
    hold_bars: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    candidate: ExternalBenchmarkCandidate | None = None,
    cost_model: BenchmarkCostModel | None = None,
) -> dict[str, AlphaArenaBacktestResult]:
    timestamps = [int(item["timestamp_ms"]) for item in candles]
    opens = [float(item["open"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    closes = [float(item["close"]) for item in candles]
    results: dict[str, AlphaArenaBacktestResult] = {}
    for group_key, signals in signal_groups.items():
        returns: list[float] = []
        for signal in signals:
            if signal.action not in {"buy", "sell"}:
                continue
            entry_index = next((idx for idx, ts in enumerate(timestamps) if ts >= signal.timestamp_ms), None)
            if entry_index is None or entry_index >= len(candles) - 1:
                continue
            realized = _simulate_signal_return_ohlc(
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                entry_index=entry_index,
                action=signal.action,
                hold_bars=hold_bars,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                candidate=candidate,
                cost_model=cost_model,
            )
            returns.append(realized)
        results[group_key] = _aggregate_returns(signal_count=len(signals), returns=returns)
    return results


def _aggregate_returns(*, signal_count: int, returns: list[float]) -> AlphaArenaBacktestResult:
    trade_count = len(returns)
    wins = [item for item in returns if item > 0]
    losses = [item for item in returns if item < 0]
    win_rate = sum(1 for item in returns if item > 0) / trade_count if trade_count else 0.0
    avg_return_pct = fmean(returns) * 100 if returns else 0.0
    cumulative_return_pct = sum(returns) * 100
    avg_win_pct = fmean(wins) * 100 if wins else 0.0
    avg_loss_pct = fmean(losses) * 100 if losses else 0.0
    expectancy_pct = (win_rate * avg_win_pct) + ((1 - win_rate) * avg_loss_pct) if trade_count else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return AlphaArenaBacktestResult(
        sample_count=signal_count,
        trade_count=trade_count,
        win_rate=win_rate,
        avg_return_pct=avg_return_pct,
        cumulative_return_pct=cumulative_return_pct,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        expectancy_pct=expectancy_pct,
        profit_factor=profit_factor,
    )


def _benchmark_sort_key(item: ExternalBenchmarkResult) -> tuple[float, float, float, int]:
    return (
        float(item.expectancy_pct),
        float(item.profit_factor),
        float(item.cumulative_return_pct),
        int(item.trade_count),
    )


def _load_alpha_arena_normalized_signals(
    normalized_dir: Path,
    *,
    symbol: str,
    max_signals: int,
) -> dict[str, list[AlphaArenaSignal]]:
    grouped: dict[str, list[AlphaArenaSignal]] = {}
    if not normalized_dir.exists():
        return grouped
    files = sorted(normalized_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("symbol", "")).strip().upper() != symbol.upper():
                continue
            signal = AlphaArenaSignal(
                timestamp_ms=int(payload.get("timestamp_ms", 0)),
                symbol=str(payload.get("symbol", symbol)).strip(),
                model=str(payload.get("model", "unknown")).strip() or "unknown",
                action=str(payload.get("action", "hold")).strip().lower(),
                confidence=float(payload.get("confidence", 0.0) or 0.0),
                commentary=str(payload.get("commentary", "")).strip(),
                source_url=str(payload.get("source_url", "")).strip(),
            )
            grouped.setdefault(signal.model, []).append(signal)
            if sum(len(items) for items in grouped.values()) >= max_signals:
                break
        if sum(len(items) for items in grouped.values()) >= max_signals:
            break
    for key in list(grouped):
        grouped[key] = sorted(grouped[key], key=lambda item: item.timestamp_ms)[-max_signals:]
    return grouped


def _discover_alpha_symbols(normalized_dir: Path, max_symbols: int = 12) -> list[str]:
    symbols: list[str] = []
    if not normalized_dir.exists():
        return symbols
    for path in sorted(normalized_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            for line in path.read_text(errors="replace").splitlines():
                if len(symbols) >= max_symbols:
                    return symbols
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                symbol = str(payload.get("symbol", "")).strip().upper()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
        except Exception:
            continue
    return symbols


def _simulate_signal_return_ohlc(
    *,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    entry_index: int,
    action: str,
    hold_bars: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    candidate: ExternalBenchmarkCandidate | None = None,
    cost_model: BenchmarkCostModel | None = None,
) -> float:
    entry_price = float(closes[entry_index])
    if entry_price <= 0:
        return 0.0
    direction = 1.0 if action == "buy" else -1.0
    profile = _resolve_benchmark_exit_profile(
        candidate=candidate,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_index=entry_index,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
    )
    take_profit_pct = profile["take_profit_pct"]
    stop_loss_pct = profile["stop_loss_pct"]
    total_cost_pct = float((cost_model or BenchmarkCostModel()).total_round_trip_cost_pct)
    take_level = entry_price * (1.0 + (take_profit_pct * direction))
    stop_level = entry_price * (1.0 - (stop_loss_pct * direction))
    exit_mode = str(profile.get("exit_mode", "fixed")).strip() or "fixed"
    midline_period = int(profile.get("midline_period", 0) or 0)
    last_return = 0.0
    for offset in range(1, max(hold_bars, 1) + 1):
        next_index = entry_index + offset
        if next_index >= len(closes):
            break
        high = float(highs[next_index])
        low = float(lows[next_index])
        close = float(closes[next_index])
        current_return = ((close - entry_price) / entry_price) * direction
        last_return = current_return
        if action == "buy":
            hit_stop = stop_loss_pct > 0 and low <= stop_level
            hit_take = take_profit_pct > 0 and high >= take_level
        else:
            hit_stop = stop_loss_pct > 0 and high >= stop_level
            hit_take = take_profit_pct > 0 and low <= take_level
        if exit_mode == "atr_midline" and midline_period > 1:
            midline = _compute_donchian_midline(highs, lows, next_index, midline_period)
            if midline > 0:
                if action == "buy" and low <= midline < entry_price:
                    return ((midline - entry_price) / entry_price) - total_cost_pct
                if action == "sell" and high >= midline > entry_price:
                    return ((entry_price - midline) / entry_price) - total_cost_pct
        if hit_stop and hit_take:
            return -stop_loss_pct - total_cost_pct
        if hit_stop:
            return -stop_loss_pct - total_cost_pct
        if hit_take:
            return take_profit_pct - total_cost_pct
    return last_return - total_cost_pct


def _rolling_mean(values: list[float]) -> float:
    return fmean(values) if values else 0.0


def _rolling_std(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = _rolling_mean(values)
    variance = sum((value - mean) ** 2 for value in values) / max(len(values), 1)
    return variance ** 0.5


def _compute_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    period = max(period, 1)
    multiplier = 2.0 / (period + 1.0)
    ema_values = [float(values[0])]
    for value in values[1:]:
        ema_values.append((float(value) * multiplier) + (ema_values[-1] * (1.0 - multiplier)))
    return ema_values


def _compute_atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    if not highs or not lows or not closes:
        return []
    period = max(period, 1)
    true_ranges: list[float] = []
    prev_close = float(closes[0])
    for high, low, close in zip(highs, lows, closes):
        high = float(high)
        low = float(low)
        close = float(close)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = close
    atr_values: list[float] = []
    rolling: list[float] = []
    atr = 0.0
    for tr in true_ranges:
        rolling.append(tr)
        if len(rolling) < period:
            atr = fmean(rolling)
        elif len(rolling) == period:
            atr = fmean(rolling)
        else:
            atr = ((atr * (period - 1)) + tr) / period
        atr_values.append(atr)
    return atr_values


def _compute_donchian_midline(highs: list[float], lows: list[float], index: int, period: int) -> float:
    if period <= 1 or index < 0:
        return 0.0
    start_index = max(0, index - period + 1)
    high_window = highs[start_index : index + 1]
    low_window = lows[start_index : index + 1]
    if not high_window or not low_window:
        return 0.0
    return (max(high_window) + min(low_window)) / 2.0


def _resolve_benchmark_exit_profile(
    *,
    candidate: ExternalBenchmarkCandidate | None,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    entry_index: int,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> dict[str, float | str]:
    profile = {
        "exit_mode": "fixed",
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "midline_period": 0,
    }
    if candidate is None or not candidate.params:
        return profile
    exit_mode = str(candidate.params.get("exit_mode", "fixed")).strip() or "fixed"
    profile["exit_mode"] = exit_mode
    if exit_mode != "atr_midline":
        return profile
    atr_period = int(candidate.params.get("atr_period", 14) or 14)
    atr_target_multiplier = float(candidate.params.get("atr_target_multiplier", 1.8) or 1.8)
    atr_stop_multiplier = float(candidate.params.get("atr_stop_multiplier", 1.2) or 1.2)
    midline_period = int(candidate.params.get("midline_period", 10) or 10)
    atr_values = _compute_atr_series(highs[: entry_index + 1], lows[: entry_index + 1], closes[: entry_index + 1], atr_period)
    atr_value = float(atr_values[-1]) if atr_values else 0.0
    entry_price = float(closes[entry_index]) if entry_index < len(closes) else 0.0
    atr_pct = (atr_value / entry_price) if entry_price > 0 else 0.0
    if atr_pct > 0:
        profile["take_profit_pct"] = min(max(atr_pct * atr_target_multiplier, atr_pct * 0.9), max(take_profit_pct, atr_pct * 1.1))
        profile["stop_loss_pct"] = min(max(atr_pct * atr_stop_multiplier, atr_pct * 0.8), max(stop_loss_pct, atr_pct * 1.05))
    profile["midline_period"] = midline_period
    return profile


def _compute_rsi(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = fmean(gains[1 : period + 1])
    avg_loss = fmean(losses[1 : period + 1])
    output = [50.0] * len(closes)
    if avg_loss == 0:
        output[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        output[period] = 100.0 - (100.0 / (1.0 + rs))
    for index in range(period + 1, len(closes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        if avg_loss == 0:
            output[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            output[index] = 100.0 - (100.0 / (1.0 + rs))
    return output


def _generate_donchian_adx_signals(
    candles: list[dict[str, float | int]],
    *,
    symbol: str,
    candidate: ExternalBenchmarkCandidate,
) -> list[AlphaArenaSignal]:
    closes = [float(item["close"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    volumes = [float(item["volume"]) for item in candles]
    channel_period = int(candidate.params.get("channel_period", 20) or 20)
    adx_period = int(candidate.params.get("adx_period", 14) or 14)
    adx_threshold = float(candidate.params.get("adx_threshold", 20.0) or 20.0)
    volume_ratio_threshold = float(candidate.params.get("volume_ratio_threshold", 1.05) or 1.05)
    signals: list[AlphaArenaSignal] = []
    start_index = max(channel_period + 1, (adx_period * 2))
    for index in range(start_index, len(closes)):
        action, metrics = donchian_adx_signal(
            highs=highs[: index + 1],
            lows=lows[: index + 1],
            closes=closes[: index + 1],
            volumes=volumes[: index + 1],
            index=index,
            channel_period=channel_period,
            adx_period=adx_period,
            adx_threshold=adx_threshold,
            volume_ratio_threshold=volume_ratio_threshold,
        )
        if action not in {"long", "short"}:
            continue
        normalized_action = "buy" if action == "long" else "sell"
        signals.append(
            AlphaArenaSignal(
                timestamp_ms=int(candles[index]["timestamp_ms"]),
                symbol=symbol,
                model=candidate.id,
                action=normalized_action,
                confidence=min(max(float(metrics.get("adx", adx_threshold)) / max(adx_threshold, 1.0), 0.0), 1.0),
                commentary=f"donchian/adx signal at bar {index}",
            )
        )
    return signals


def _generate_donchian_adx_keltner_signals(
    candles: list[dict[str, float | int]],
    *,
    symbol: str,
    candidate: ExternalBenchmarkCandidate,
) -> list[AlphaArenaSignal]:
    closes = [float(item["close"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    volumes = [float(item["volume"]) for item in candles]
    channel_period = int(candidate.params.get("channel_period", 14) or 14)
    adx_period = int(candidate.params.get("adx_period", 14) or 14)
    adx_threshold = float(candidate.params.get("adx_threshold", 19.0) or 19.0)
    volume_ratio_threshold = float(candidate.params.get("volume_ratio_threshold", 1.15) or 1.15)
    keltner_period = int(candidate.params.get("keltner_period", 20) or 20)
    keltner_atr_period = int(candidate.params.get("keltner_atr_period", 14) or 14)
    keltner_atr_multiplier = float(candidate.params.get("keltner_atr_multiplier", 1.4) or 1.4)
    ema_values = _compute_ema(closes, keltner_period)
    atr_values = _compute_atr_series(highs, lows, closes, keltner_atr_period)
    signals: list[AlphaArenaSignal] = []
    start_index = max(channel_period + 1, adx_period * 2, keltner_period, keltner_atr_period)
    for index in range(start_index, len(closes)):
        action, metrics = donchian_adx_signal(
            highs=highs[: index + 1],
            lows=lows[: index + 1],
            closes=closes[: index + 1],
            volumes=volumes[: index + 1],
            index=index,
            channel_period=channel_period,
            adx_period=adx_period,
            adx_threshold=adx_threshold,
            volume_ratio_threshold=volume_ratio_threshold,
        )
        if action not in {"long", "short"}:
            continue
        ema_value = float(ema_values[index]) if index < len(ema_values) else 0.0
        atr_value = float(atr_values[index]) if index < len(atr_values) else 0.0
        if ema_value <= 0 or atr_value <= 0:
            continue
        keltner_upper = ema_value + (atr_value * keltner_atr_multiplier)
        keltner_lower = ema_value - (atr_value * keltner_atr_multiplier)
        close = closes[index]
        if action == "long" and close <= keltner_upper:
            continue
        if action == "short" and close >= keltner_lower:
            continue
        normalized_action = "buy" if action == "long" else "sell"
        signals.append(
            AlphaArenaSignal(
                timestamp_ms=int(candles[index]["timestamp_ms"]),
                symbol=symbol,
                model=candidate.id,
                action=normalized_action,
                confidence=min(max(float(metrics.get("adx", adx_threshold)) / max(adx_threshold, 1.0), 0.0), 1.0),
                commentary=f"donchian/adx + keltner filter signal at bar {index}",
            )
        )
    return signals


def _generate_grid_range_reversion_signals(
    candles: list[dict[str, float | int]],
    *,
    symbol: str,
    candidate: ExternalBenchmarkCandidate,
) -> list[AlphaArenaSignal]:
    closes = [float(item["close"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    bb_period = int(candidate.params.get("bb_period", 20) or 20)
    bb_stddev = float(candidate.params.get("bb_stddev", 2.0) or 2.0)
    adx_period = int(candidate.params.get("adx_period", 14) or 14)
    adx_max = float(candidate.params.get("adx_max", 18.0) or 18.0)
    bb_width_min_pct = float(candidate.params.get("bb_width_min_pct", 0.60) or 0.60)
    bb_width_max_pct = float(candidate.params.get("bb_width_max_pct", 4.50) or 4.50)
    entry_band_buffer_pct = float(candidate.params.get("entry_band_buffer_pct", 0.15) or 0.15) / 100.0
    rsi_period = int(candidate.params.get("rsi_period", 14) or 14)
    rsi_buy_max = float(candidate.params.get("rsi_buy_max", 43.0) or 43.0)
    rsi_sell_min = float(candidate.params.get("rsi_sell_min", 57.0) or 57.0)
    signal_cooldown = int(candidate.params.get("signal_cooldown_bars", 2) or 2)
    adx_state = compute_adx(highs, lows, closes, period=adx_period)
    adx_values = adx_state.get("adx", [])
    rsi_values = _compute_rsi(closes, period=rsi_period)
    signals: list[AlphaArenaSignal] = []
    next_allowed_index = 0
    for index in range(max(bb_period, adx_period * 2), len(closes)):
        if index < next_allowed_index:
            continue
        window = closes[index - bb_period + 1 : index + 1]
        middle = _rolling_mean(window)
        std = _rolling_std(window)
        upper = middle + (std * bb_stddev)
        lower = middle - (std * bb_stddev)
        if middle <= 0 or upper <= 0 or lower <= 0:
            continue
        width_pct = ((upper - lower) / middle) * 100.0
        adx_value = float(adx_values[index]) if index < len(adx_values) else 0.0
        close = closes[index]
        rsi = float(rsi_values[index]) if index < len(rsi_values) else 50.0
        near_lower = close <= lower * (1.0 + entry_band_buffer_pct)
        near_upper = close >= upper * (1.0 - entry_band_buffer_pct)
        in_range = bb_width_min_pct <= width_pct <= bb_width_max_pct and adx_value <= adx_max
        action = "hold"
        if in_range and near_lower and rsi <= rsi_buy_max:
            action = "buy"
        elif in_range and near_upper and rsi >= rsi_sell_min:
            action = "sell"
        if action == "hold":
            continue
        signals.append(
            AlphaArenaSignal(
                timestamp_ms=int(candles[index]["timestamp_ms"]),
                symbol=symbol,
                model=candidate.id,
                action=action,
                confidence=min(max((adx_max - adx_value) / max(adx_max, 1.0), 0.0), 1.0),
                commentary=f"grid-range reversion signal at bar {index}",
            )
        )
        next_allowed_index = index + max(signal_cooldown, 1)
    return signals


def _generate_bollinger_rsi_signals(
    candles: list[dict[str, float | int]],
    *,
    symbol: str,
    candidate: ExternalBenchmarkCandidate,
) -> list[AlphaArenaSignal]:
    closes = [float(item["close"]) for item in candles]
    bb_period = int(candidate.params.get("bb_period", 20) or 20)
    bb_stddev = float(candidate.params.get("bb_stddev", 2.0) or 2.0)
    rsi_period = int(candidate.params.get("rsi_period", 14) or 14)
    rsi_buy_max = float(candidate.params.get("rsi_buy_max", 35.0) or 35.0)
    rsi_sell_min = float(candidate.params.get("rsi_sell_min", 65.0) or 65.0)
    signal_cooldown = int(candidate.params.get("signal_cooldown_bars", 2) or 2)
    rsi_values = _compute_rsi(closes, period=rsi_period)
    signals: list[AlphaArenaSignal] = []
    next_allowed_index = 0
    for index in range(max(bb_period, rsi_period + 1), len(closes)):
        if index < next_allowed_index:
            continue
        window = closes[index - bb_period + 1 : index + 1]
        middle = _rolling_mean(window)
        std = _rolling_std(window)
        upper = middle + (std * bb_stddev)
        lower = middle - (std * bb_stddev)
        close = closes[index]
        rsi = float(rsi_values[index]) if index < len(rsi_values) else 50.0
        action = "hold"
        if close <= lower and rsi <= rsi_buy_max:
            action = "buy"
        elif close >= upper and rsi >= rsi_sell_min:
            action = "sell"
        if action == "hold":
            continue
        signals.append(
            AlphaArenaSignal(
                timestamp_ms=int(candles[index]["timestamp_ms"]),
                symbol=symbol,
                model=candidate.id,
                action=action,
                confidence=abs(rsi - 50.0) / 50.0,
                commentary=f"bollinger/rsi mean reversion signal at bar {index}",
            )
        )
        next_allowed_index = index + max(signal_cooldown, 1)
    return signals


def _generate_bollinger_keltner_extreme_reversion_signals(
    candles: list[dict[str, float | int]],
    *,
    symbol: str,
    candidate: ExternalBenchmarkCandidate,
) -> list[AlphaArenaSignal]:
    closes = [float(item["close"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    bb_period = int(candidate.params.get("bb_period", 20) or 20)
    bb_stddev = float(candidate.params.get("bb_stddev", 2.5) or 2.5)
    keltner_period = int(candidate.params.get("keltner_period", 20) or 20)
    keltner_atr_period = int(candidate.params.get("keltner_atr_period", 14) or 14)
    keltner_atr_multiplier = float(candidate.params.get("keltner_atr_multiplier", 1.8) or 1.8)
    rsi_period = int(candidate.params.get("rsi_period", 14) or 14)
    rsi_buy_max = float(candidate.params.get("rsi_buy_max", 30.0) or 30.0)
    rsi_sell_min = float(candidate.params.get("rsi_sell_min", 70.0) or 70.0)
    signal_cooldown = int(candidate.params.get("signal_cooldown_bars", 3) or 3)
    ema_values = _compute_ema(closes, keltner_period)
    atr_values = _compute_atr_series(highs, lows, closes, keltner_atr_period)
    rsi_values = _compute_rsi(closes, period=rsi_period)
    signals: list[AlphaArenaSignal] = []
    next_allowed_index = 0
    start_index = max(bb_period, keltner_period, keltner_atr_period, rsi_period + 1)
    for index in range(start_index, len(closes)):
        if index < next_allowed_index:
            continue
        window = closes[index - bb_period + 1 : index + 1]
        middle = _rolling_mean(window)
        std = _rolling_std(window)
        ema_value = float(ema_values[index]) if index < len(ema_values) else 0.0
        atr_value = float(atr_values[index]) if index < len(atr_values) else 0.0
        if middle <= 0 or std <= 0 or ema_value <= 0 or atr_value <= 0:
            continue
        bb_upper = middle + (std * bb_stddev)
        bb_lower = middle - (std * bb_stddev)
        kc_upper = ema_value + (atr_value * keltner_atr_multiplier)
        kc_lower = ema_value - (atr_value * keltner_atr_multiplier)
        close = closes[index]
        rsi = float(rsi_values[index]) if index < len(rsi_values) else 50.0
        action = "hold"
        if close <= bb_lower and close <= kc_lower and rsi <= rsi_buy_max:
            action = "buy"
        elif close >= bb_upper and close >= kc_upper and rsi >= rsi_sell_min:
            action = "sell"
        if action == "hold":
            continue
        confidence = min(max(abs(rsi - 50.0) / 50.0, 0.0), 1.0)
        signals.append(
            AlphaArenaSignal(
                timestamp_ms=int(candles[index]["timestamp_ms"]),
                symbol=symbol,
                model=candidate.id,
                action=action,
                confidence=confidence,
                commentary=f"bollinger+keltner extreme reversion signal at bar {index}",
            )
        )
        next_allowed_index = index + max(signal_cooldown, 1)
    return signals


_RULE_GENERATORS: dict[str, Callable[..., list[AlphaArenaSignal]]] = {
    "donchian_adx": _generate_donchian_adx_signals,
    "donchian_adx_keltner": _generate_donchian_adx_keltner_signals,
    "grid_range_reversion": _generate_grid_range_reversion_signals,
    "bollinger_rsi_mean_reversion": _generate_bollinger_rsi_signals,
    "bollinger_keltner_extreme_reversion": _generate_bollinger_keltner_extreme_reversion_signals,
}
