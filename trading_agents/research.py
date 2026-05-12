from __future__ import annotations

import json
from pathlib import Path
from statistics import fmean
from typing import Any

from trading_agents.backtest import build_backtest_snapshot
from trading_agents.external_benchmarks import (
    AlphaArenaBacktestResult,
    BenchmarkCostModel,
    ExternalBenchmarkCandidate,
    _RULE_GENERATORS,
    benchmark_signal_groups,
    build_benchmark_cost_model,
)
from trading_agents.llm import OllamaClient
from trading_agents.models import (
    BacktestSnapshot,
    MarketSnapshot,
    SentimentSnapshot,
    StrategyCandidate,
    StrategyResearchSnapshot,
)


def _snapshot_to_candles(snapshot: MarketSnapshot) -> list[dict[str, float | int]]:
    candles: list[dict[str, float | int]] = []
    for index, (open_, high, low, close, volume) in enumerate(
        zip(snapshot.opens, snapshot.highs, snapshot.lows, snapshot.closes, snapshot.volumes)
    ):
        candles.append(
            {
                "timestamp_ms": index * 60_000,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
        )
    return candles


def _benchmark_candidate_from_strategy(item: dict[str, Any]) -> ExternalBenchmarkCandidate | None:
    strategy_id = str(item.get("id", "")).strip()
    generator = str(item.get("generator", "")).strip()
    if not strategy_id or not generator:
        return None
    return ExternalBenchmarkCandidate(
        id=strategy_id,
        name=str(item.get("name", strategy_id)).strip() or strategy_id,
        kind=str(item.get("kind", "rule_strategy")).strip() or "rule_strategy",
        generator=generator,
        source=str(item.get("source", "research")).strip() or "research",
        description=str(item.get("description", "")).strip(),
        hold_bars=max(int(item.get("hold_bars", 4) or 4), 1),
        take_profit_pct=float(item.get("take_profit_pct", 0.0) or 0.0) / 100.0,
        stop_loss_pct=float(item.get("stop_loss_pct", 0.0) or 0.0) / 100.0,
        params=item.get("params", {}) if isinstance(item.get("params"), dict) else {},
    )


def _backtest_from_benchmark_result(
    *,
    strategy_id: str,
    signal_count: int,
    result: AlphaArenaBacktestResult | None,
    empty_summary: str,
) -> BacktestSnapshot:
    if result is None:
        return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, empty_summary, 0.0, 0.0, 0.0, 0.0)
    returns: list[float] = []
    if result.trade_count > 0:
        win_count = int(round(result.win_rate * result.trade_count))
        win_count = max(0, min(win_count, result.trade_count))
        loss_count = max(result.trade_count - win_count, 0)
        returns.extend([result.avg_win_pct / 100.0] * win_count if result.avg_win_pct != 0 else [])
        returns.extend([result.avg_loss_pct / 100.0] * loss_count if result.avg_loss_pct != 0 else [])
        while len(returns) < result.trade_count:
            returns.append(result.avg_return_pct / 100.0)
    return build_backtest_snapshot(
        sample_count=max(signal_count, result.sample_count),
        returns=returns,
        summary_prefix=strategy_id,
        empty_summary=empty_summary,
    )


class StrategyResearchAgent:
    name = "strategy_researcher"

    def __init__(
        self,
        library_path: str,
        *,
        settings: Any | None = None,
        llm_client: OllamaClient | None = None,
    ) -> None:
        self.library_path = Path(library_path)
        self.settings = settings
        self.library = self._load_library()
        self.llm_client = llm_client

    def _load_library(self) -> dict:
        if not self.library_path.exists():
            return {"base_strategy": "donchian_adx_perp_v1", "strategies": []}
        return json.loads(self.library_path.read_text())

    def evaluate(self, snapshot: MarketSnapshot, sentiment: SentimentSnapshot) -> StrategyResearchSnapshot:
        return self.evaluate_with_memory(snapshot, sentiment, strategy_memory=None)

    def evaluate_with_memory(
        self,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
        strategy_memory: dict | None = None,
    ) -> StrategyResearchSnapshot:
        base_id = self.library.get("base_strategy", "donchian_adx_perp_v1")
        raw_strategies = [item for item in self.library.get("strategies", []) if isinstance(item, dict)]
        candidates: list[StrategyCandidate] = []
        candidate_items: dict[str, dict[str, Any]] = {}
        for item in raw_strategies:
            strategy_id = str(item.get("id", "")).strip()
            if not strategy_id:
                continue
            backtest = self._run_strategy(item, snapshot, sentiment)
            candidates.append(
                StrategyCandidate(
                    strategy_id=strategy_id,
                    name=str(item.get("name", strategy_id)).strip() or strategy_id,
                    source=str(item.get("source", "research")).strip() or "research",
                    credibility=str(item.get("credibility", "external_public")).strip() or "external_public",
                    description=str(item.get("description", "")).strip(),
                    backtest=backtest,
                )
            )
            candidate_items[strategy_id] = item
        if not candidates:
            fallback = StrategyCandidate(
                strategy_id=base_id,
                name="Fallback Strategy",
                source="public_classic",
                credibility="external_public",
                description="Fallback strategy when no strategy candidates are loaded.",
                backtest=BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no strategy candidates loaded"),
            )
            candidates = [fallback]
            candidate_items[base_id] = {"id": base_id, "name": fallback.name, "execution": {"entry_order_type": "market"}}

        selected, selected_item, selection_mode = self._select_candidate_with_memory(
            candidates,
            candidate_items,
            strategy_memory,
        )
        current_signal, current_metrics = self._current_signal(selected_item, snapshot)
        rationale = self._fallback_rationale(
            base_id,
            selected,
            current_signal,
            current_metrics,
            selection_mode=selection_mode,
            strategy_memory=strategy_memory,
        )
        execution_profile = self._execution_profile(selected_item)
        summary = (
            f"selected strategy {selected.strategy_id}; "
            f"base={base_id}; {selected.backtest.summary}; "
            f"selection_mode={selection_mode}; "
            f"current_signal={current_signal}; "
            f"current_signal_type={current_metrics.get('signal_type', 'hold')}; "
            f"current_adx={current_metrics.get('adx', 0.0):.2f}; "
            f"current_volume_ratio={current_metrics.get('volume_ratio', 0.0):.2f}; "
            f"execution={execution_profile.get('entry_order_type', 'market')}/{execution_profile.get('entry_liquidity', 'taker')}; "
            f"rationale={rationale}"
        )
        return StrategyResearchSnapshot(
            base_strategy_id=base_id,
            selected_strategy_id=selected.strategy_id,
            selected_strategy_name=selected.name,
            summary=summary,
            candidates=candidates,
            selected_strategy_rationale=rationale,
            selected_execution_profile=execution_profile,
            current_signal=current_signal,
            current_signal_type=str(current_metrics.get("signal_type", "hold") or "hold"),
            current_adx=float(current_metrics.get("adx", 0.0) or 0.0),
            current_volume_ratio=float(current_metrics.get("volume_ratio", 0.0) or 0.0),
        )

    def _execution_profile(self, strategy_item: dict[str, Any]) -> dict[str, object]:
        execution = strategy_item.get("execution", {})
        if not isinstance(execution, dict):
            execution = {}
        entry_order_type = str(execution.get("entry_order_type", "market") or "market").strip().lower()
        entry_liquidity = str(execution.get("entry_liquidity", "taker") or "taker").strip().lower()
        post_only = bool(execution.get("post_only", entry_order_type == "limit"))
        passive_offset_bps = float(execution.get("passive_offset_bps", 0.0) or 0.0)
        entry_ttl_seconds = int(execution.get("entry_ttl_seconds", 20) or 20)
        return {
            "entry_order_type": entry_order_type,
            "entry_liquidity": entry_liquidity,
            "post_only": post_only,
            "passive_offset_bps": passive_offset_bps,
            "entry_ttl_seconds": max(entry_ttl_seconds, 1),
        }

    def _fallback_rationale(
        self,
        base_id: str,
        selected: StrategyCandidate,
        current_signal: str,
        current_metrics: dict[str, float],
        *,
        selection_mode: str,
        strategy_memory: dict | None,
    ) -> str:
        signal_type = str(current_metrics.get("signal_type", "hold") or "hold")
        adx = float(current_metrics.get("adx", 0.0) or 0.0)
        volume_ratio = float(current_metrics.get("volume_ratio", 0.0) or 0.0)
        controls = (strategy_memory or {}).get("controls") if isinstance(strategy_memory, dict) else {}
        controls = controls if isinstance(controls, dict) else {}
        return (
            "single external strategy mode is active; "
            f"base_strategy={base_id}; "
            f"selected={selected.strategy_id}; "
            f"selection_mode={selection_mode}; "
            f"live_signal={current_signal}; "
            f"signal_type={signal_type}; "
            f"adx={adx:.2f}; "
            f"volume_ratio={volume_ratio:.2f}; "
            f"benchmark_watch_candidate={str(controls.get('benchmark_watch_candidate', '') or '').strip()}; "
            f"pilot_candidate_id={str(controls.get('pilot_candidate_id', '') or '').strip()}"
        )

    def _select_candidate_with_memory(
        self,
        candidates: list[StrategyCandidate],
        candidate_items: dict[str, dict[str, Any]],
        strategy_memory: dict | None,
    ) -> tuple[StrategyCandidate, dict[str, Any], str]:
        if not candidates:
            raise ValueError("at least one strategy candidate is required")
        controls = (strategy_memory or {}).get("controls") if isinstance(strategy_memory, dict) else {}
        controls = controls if isinstance(controls, dict) else {}
        pilot_candidate_id = str(controls.get("pilot_candidate_id", "") or "").strip()
        benchmark_watch_candidate = str(controls.get("benchmark_watch_candidate", "") or "").strip()
        entry_mode = str(controls.get("entry_mode", "") or "").strip().lower()
        carry_in_mode = str(controls.get("carry_in_mode", "") or "").strip().lower()

        def _score(candidate: StrategyCandidate) -> float:
            score = float(candidate.backtest.expectancy_pct or 0.0) * 8.0
            score += float(candidate.backtest.profit_factor or 0.0) * 0.2
            score += float(candidate.backtest.cumulative_return_pct or 0.0) * 0.02
            strategy_item = candidate_items.get(candidate.strategy_id, {})
            execution_profile = self._execution_profile(strategy_item)
            if candidate.strategy_id == pilot_candidate_id:
                score += 1.0
            if candidate.strategy_id == benchmark_watch_candidate:
                score += 0.5
            if entry_mode == "capital_preservation_pilot" and candidate.strategy_id == pilot_candidate_id:
                score += 0.5
            if execution_profile.get("entry_liquidity") == "maker":
                score += 0.08
            if carry_in_mode == "de_risk":
                hold_bars = float(strategy_item.get("hold_bars", 0.0) or 0.0)
                if hold_bars > 0:
                    score += max(0.0, (8.0 - hold_bars)) * 0.02
            return score

        ranked = sorted(candidates, key=_score, reverse=True)
        selected = ranked[0]
        selection_mode = "memory_ranked"
        if pilot_candidate_id and selected.strategy_id == pilot_candidate_id:
            selection_mode = "memory_pilot_aligned"
        elif benchmark_watch_candidate and selected.strategy_id == benchmark_watch_candidate:
            selection_mode = "memory_benchmark_aligned"
        return selected, candidate_items.get(selected.strategy_id, {}), selection_mode

    def _current_signal(self, strategy_item: dict[str, Any], snapshot: MarketSnapshot) -> tuple[str, dict[str, float]]:
        candidate = _benchmark_candidate_from_strategy(strategy_item)
        if candidate is None:
            return "hold", {}
        generator = _RULE_GENERATORS.get(candidate.generator) or _RULE_GENERATORS.get(candidate.kind) or _RULE_GENERATORS.get(candidate.id)
        if generator is None:
            return "hold", {}
        candles = _snapshot_to_candles(snapshot)
        if len(candles) < 35:
            return "hold", {}
        signals = generator(candles, symbol=snapshot.symbol, candidate=candidate)
        last_timestamp = int(candles[-1]["timestamp_ms"])
        recent_signal = next((item for item in reversed(signals) if int(item.timestamp_ms) == last_timestamp), None)
        adx_value = self._latest_adx(snapshot, candidate)
        volume_ratio = self._latest_volume_ratio(snapshot)
        metrics = {
            "signal_type": "hold",
            "adx": adx_value,
            "volume_ratio": volume_ratio,
        }
        if recent_signal is None or recent_signal.action not in {"buy", "sell"}:
            return "hold", metrics
        metrics["signal_type"] = f"{candidate.generator}_{recent_signal.action}"
        return ("long" if recent_signal.action == "buy" else "short"), metrics

    def _run_strategy(
        self,
        strategy_item: dict[str, Any],
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
    ) -> BacktestSnapshot:
        strategy_id = str(strategy_item.get("id", "")).strip() or "unknown_strategy"
        candidate = _benchmark_candidate_from_strategy(strategy_item)
        if candidate is None:
            return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, f"{strategy_id}: invalid strategy candidate")
        generator = _RULE_GENERATORS.get(candidate.generator) or _RULE_GENERATORS.get(candidate.kind) or _RULE_GENERATORS.get(candidate.id)
        if generator is None:
            return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, f"{strategy_id}: no generator registered")
        candles = _snapshot_to_candles(snapshot)
        if len(candles) < 35:
            return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, f"{strategy_id}: not enough candles")
        signals = generator(candles, symbol=snapshot.symbol, candidate=candidate)
        # Keep a light sentiment veto for extreme shock conditions only.
        if signals:
            if sentiment.sentiment_score <= -0.90:
                signals = [item for item in signals if item.action != "buy"]
            elif sentiment.sentiment_score >= 0.90:
                signals = [item for item in signals if item.action != "sell"]
        if self.settings is not None:
            cost_model = build_benchmark_cost_model(self.settings, candidate)
        else:
            cost_model = BenchmarkCostModel()
        result = benchmark_signal_groups(
            candles,
            {candidate.id: signals},
            hold_bars=candidate.hold_bars,
            take_profit_pct=candidate.take_profit_pct,
            stop_loss_pct=candidate.stop_loss_pct,
            candidate=candidate,
            cost_model=cost_model,
        ).get(candidate.id)
        return _backtest_from_benchmark_result(
            strategy_id=strategy_id,
            signal_count=len(signals),
            result=result,
            empty_summary=f"{strategy_id}: no valid setups in recent replay",
        )

    def _latest_adx(self, snapshot: MarketSnapshot, candidate: ExternalBenchmarkCandidate) -> float:
        adx_period = int(candidate.params.get("adx_period", 14) or 14)
        highs = snapshot.highs or snapshot.closes
        lows = snapshot.lows or snapshot.closes
        closes = snapshot.closes
        from trading_agents.backtest import compute_adx

        adx_state = compute_adx(highs, lows, closes, period=adx_period)
        if not adx_state.get("adx"):
            return 0.0
        return float(adx_state["adx"][-1])

    def _latest_volume_ratio(self, snapshot: MarketSnapshot) -> float:
        volumes = snapshot.volumes or []
        if not volumes:
            return 0.0
        recent_volume = fmean(volumes[max(0, len(volumes) - 3) :])
        baseline_volume = fmean(volumes[max(0, len(volumes) - 20) :])
        if baseline_volume <= 0:
            return 0.0
        return recent_volume / baseline_volume
