from __future__ import annotations

import json
from pathlib import Path
from statistics import fmean

from trading_agents.backtest import build_backtest_snapshot
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
            return {"base_strategy": "momentum_sentiment_v1", "strategies": []}
        return json.loads(self.library_path.read_text())

    def evaluate(self, snapshot: MarketSnapshot, sentiment: SentimentSnapshot) -> StrategyResearchSnapshot:
        return self.evaluate_with_memory(snapshot, sentiment, strategy_memory=None)

    def evaluate_with_memory(
        self,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
        strategy_memory: dict | None = None,
    ) -> StrategyResearchSnapshot:
        base_id = self.library.get("base_strategy", "momentum_sentiment_v1")
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
                name="Momentum + Sentiment",
                source="local_baseline",
                credibility="internal",
                description="Fallback strategy library entry",
                backtest=BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no strategy candidates loaded"),
            )
            candidates = [fallback]
        fallback_selected = max(
            candidates,
            key=lambda item: (
                item.backtest.trade_count > 0,
                item.backtest.expectancy_pct,
                item.backtest.profit_factor,
                item.backtest.cumulative_return_pct,
                item.backtest.trade_count,
            ),
        )
        selected = fallback_selected
        rationale = self._fallback_rationale(base_id, selected)
        llm_choice = self._llm_select(snapshot, sentiment, candidates, fallback_selected, strategy_memory)
        if llm_choice is not None:
            selected = llm_choice[0]
            rationale = llm_choice[1]
        summary = (
            f"selected strategy {selected.strategy_id}; "
            f"base={base_id}; {selected.backtest.summary}; "
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

    def _fallback_rationale(self, base_id: str, selected: StrategyCandidate) -> str:
        if selected.strategy_id == base_id:
            return "base strategy remained the most reliable candidate on current replay statistics"
        return (
            f"{selected.strategy_id} had the strongest replay edge by expectancy/profit factor "
            f"among loaded strategies"
        )

    def _llm_select(
        self,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
        candidates: list[StrategyCandidate],
        fallback_selected: StrategyCandidate,
        strategy_memory: dict | None,
    ) -> tuple[StrategyCandidate, str] | None:
        if self.llm_client is None:
            return None
        candidate_payload = []
        for item in candidates:
            candidate_payload.append(
                {
                    "strategy_id": item.strategy_id,
                    "name": item.name,
                    "description": item.description,
                    "source": item.source,
                    "credibility": item.credibility,
                    "trade_count": item.backtest.trade_count,
                    "win_rate": round(item.backtest.win_rate, 4),
                    "avg_return_pct": round(item.backtest.avg_return_pct, 4),
                    "avg_win_pct": round(item.backtest.avg_win_pct, 4),
                    "avg_loss_pct": round(item.backtest.avg_loss_pct, 4),
                    "expectancy_pct": round(item.backtest.expectancy_pct, 4),
                    "profit_factor": round(item.backtest.profit_factor, 4),
                    "cumulative_return_pct": round(item.backtest.cumulative_return_pct, 4),
                    "summary": item.backtest.summary,
                }
            )
        strategy_memory = strategy_memory or {}
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the strategy researcher in a crypto trading system. "
                    "Choose the best strategy candidate for the current symbol. "
                    "Return JSON with keys selected_strategy_id and rationale. "
                    "Prefer strategies with positive expectancy, acceptable profit factor, enough replay trades, "
                    "and a payoff profile that can still be attractive even if win rate is not dominant. "
                    "Do not pick a strategy with zero replay trades unless every candidate has zero trades. "
                    f"symbol={snapshot.symbol}; timeframe={snapshot.timeframe}; "
                    f"last_price={snapshot.last_price:.4f}; sentiment_score={sentiment.sentiment_score:+.2f}; "
                    f"sentiment_summary={sentiment.summary}; "
                    f"strategy_memory_summary={strategy_memory.get('summary', '')}; "
                    f"strategy_memory_biases={json.dumps(strategy_memory.get('biases', []), ensure_ascii=False)}; "
                    f"strategy_memory_focus_symbols={json.dumps(strategy_memory.get('focus_symbols', []), ensure_ascii=False)}; "
                    f"fallback_selected={fallback_selected.strategy_id}; "
                    f"candidates={json.dumps(candidate_payload, ensure_ascii=False)}"
                )
            )
        except Exception:
            return None
        selected_id = str(response.get("selected_strategy_id", fallback_selected.strategy_id))
        rationale = str(response.get("rationale", "")).strip()
        chosen = next((item for item in candidates if item.strategy_id == selected_id), None)
        if chosen is None:
            return None
        if chosen.backtest.trade_count == 0 and any(item.backtest.trade_count > 0 for item in candidates):
            return None
        if not rationale:
            rationale = self._fallback_rationale(self.library.get("base_strategy", "momentum_sentiment_v1"), chosen)
        return chosen, rationale

    def _run_strategy(
        self,
        strategy_id: str,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
    ) -> BacktestSnapshot:
        closes = snapshot.closes
        volumes = snapshot.volumes
        if len(closes) < 26:
            return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, f"{strategy_id}: not enough candles")

        returns: list[float] = []
        sample_count = max(len(closes) - 21, 0)
        for index in range(20, len(closes) - 1):
            short_avg = fmean(closes[index - 4 : index + 1])
            long_avg = fmean(closes[index - 19 : index + 1])
            if not long_avg:
                continue
            momentum = (short_avg - long_avg) / long_avg
            next_return = (closes[index + 1] - closes[index]) / closes[index]
            recent_volume = fmean(volumes[max(0, index - 4) : index + 1])
            longer_volume = fmean(volumes[max(0, index - 19) : index + 1])

            triggered = False
            directional_return = 0.0

            if strategy_id == "momentum_sentiment_v1":
                if momentum > 0.002 and sentiment.sentiment_score >= -0.35:
                    triggered = True
                    directional_return = next_return
                elif momentum < -0.002 and sentiment.sentiment_score <= 0.45:
                    triggered = True
                    directional_return = -next_return
            elif strategy_id == "trend_pullback_v1":
                pullback = (closes[index] - closes[index - 3]) / closes[index - 3]
                if momentum > 0.0015 and -0.02 <= pullback <= -0.002:
                    triggered = True
                    directional_return = next_return
                elif momentum < -0.0015 and 0.002 <= pullback <= 0.02:
                    triggered = True
                    directional_return = -next_return
            elif strategy_id == "breakout_volume_v1":
                rolling_high = max(closes[index - 9 : index + 1])
                rolling_low = min(closes[index - 9 : index + 1])
                if closes[index] >= rolling_high and recent_volume > longer_volume * 1.1:
                    triggered = True
                    directional_return = next_return
                elif closes[index] <= rolling_low and recent_volume > longer_volume * 1.1:
                    triggered = True
                    directional_return = -next_return

            if triggered:
                returns.append(directional_return)

        return build_backtest_snapshot(
            sample_count=sample_count,
            returns=returns,
            summary_prefix=strategy_id,
            empty_summary=f"{strategy_id}: no valid setups in recent replay",
        )
