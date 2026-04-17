from __future__ import annotations

import json
from pathlib import Path

from trading_agents.backtest import build_backtest_snapshot, compute_adx, donchian_adx_signal, _simulate_intraday_trade
from trading_agents.llm import OllamaClient
from trading_agents.models import (
    BacktestSnapshot,
    MarketSnapshot,
    SentimentSnapshot,
    StrategyCandidate,
    StrategyResearchSnapshot,
)


class StrategyResearchAgent:
    name = "strategy_researcher"

    def __init__(self, library_path: str, llm_client: OllamaClient | None = None) -> None:
        self.library_path = Path(library_path)
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
        candidates: list[StrategyCandidate] = []
        for item in self.library.get("strategies", []):
            backtest = self._run_strategy(item["id"], snapshot, sentiment)
            candidates.append(
                StrategyCandidate(
                    strategy_id=item["id"],
                    name=item["name"],
                    source=item["source"],
                    credibility=item["credibility"],
                    description=item["description"],
                    backtest=backtest,
                )
            )
        if not candidates:
            fallback = StrategyCandidate(
                strategy_id=base_id,
                name="Donchian ADX Perp",
                source="public_classic",
                credibility="external_public",
                description="Fallback external Donchian/ADX strategy entry",
                backtest=BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no strategy candidates loaded"),
            )
            candidates = [fallback]

        # Strategy reset: use a single external strategy as the live source of truth.
        selected = candidates[0]
        current_signal, current_metrics = self._current_signal(snapshot)
        rationale = self._fallback_rationale(base_id, selected, snapshot, current_signal)
        summary = (
            f"selected strategy {selected.strategy_id}; "
            f"base={base_id}; {selected.backtest.summary}; "
            f"current_signal={current_signal}; "
            f"current_adx={current_metrics.get('adx', 0.0):.2f}; "
            f"current_volume_ratio={current_metrics.get('volume_ratio', 0.0):.2f}; "
            f"rationale={rationale}"
        )
        return StrategyResearchSnapshot(
            base_strategy_id=base_id,
            selected_strategy_id=selected.strategy_id,
            selected_strategy_name=selected.name,
            summary=summary,
            candidates=candidates,
            selected_strategy_rationale=rationale,
        )

    def _fallback_rationale(self, base_id: str, selected: StrategyCandidate, snapshot: MarketSnapshot, current_signal: str) -> str:
        highs = snapshot.highs or snapshot.closes
        lows = snapshot.lows or snapshot.closes
        closes = snapshot.closes
        adx_state = compute_adx(highs, lows, closes, period=14)
        latest_adx = adx_state["adx"][-1] if adx_state["adx"] else 0.0
        return (
            "single external strategy mode is active; "
            f"base_strategy={base_id}; "
            f"selected={selected.strategy_id}; "
            f"live_signal={current_signal}; "
            f"latest_adx={latest_adx:.2f}; "
            "this strategy is based on public Donchian breakout rules with Wilder ADX trend filtering"
        )

    def _current_signal(self, snapshot: MarketSnapshot) -> tuple[str, dict[str, float]]:
        closes = snapshot.closes
        highs = snapshot.highs or closes
        lows = snapshot.lows or closes
        volumes = snapshot.volumes
        if len(closes) < 35:
            return "hold", {}
        return donchian_adx_signal(
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            index=len(closes) - 1,
            channel_period=20,
            adx_period=14,
            adx_threshold=20.0,
            volume_ratio_threshold=1.10,
        )

    def _run_strategy(
        self,
        strategy_id: str,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
    ) -> BacktestSnapshot:
        closes = snapshot.closes
        highs = snapshot.highs or closes
        lows = snapshot.lows or closes
        volumes = snapshot.volumes
        if len(closes) < 35:
            return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, f"{strategy_id}: not enough candles")

        returns: list[float] = []
        start_index = 28
        for index in range(start_index, len(closes) - 6):
            direction, metrics = donchian_adx_signal(
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes,
                index=index,
                channel_period=20,
                adx_period=14,
                adx_threshold=20.0,
                volume_ratio_threshold=1.10,
            )
            if direction == "hold":
                continue
            # Keep a light sentiment veto so the external rule remains dominant,
            # while still avoiding obvious sentiment shocks.
            if direction == "long" and sentiment.sentiment_score < -0.85:
                continue
            if direction == "short" and sentiment.sentiment_score > 0.85:
                continue

            atr_pct = (metrics.get("atr", 0.0) / closes[index]) if closes[index] > 0 else 0.0
            stop_loss_pct = max(atr_pct * 1.0, 0.0045)
            take_profit_pct = max(atr_pct * 1.8, stop_loss_pct * 1.6)
            returns.append(
                _simulate_intraday_trade(
                    closes,
                    entry_index=index,
                    direction=direction,
                    max_hold_bars=6,
                    take_profit_pct=take_profit_pct,
                    stop_loss_pct=stop_loss_pct,
                )
            )

        sample_count = max(len(closes) - start_index, 0)
        return build_backtest_snapshot(
            sample_count=sample_count,
            returns=returns,
            summary_prefix=strategy_id,
            empty_summary=f"{strategy_id}: no valid setups in recent replay",
        )
