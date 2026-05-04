from __future__ import annotations

import json
from statistics import fmean
from typing import cast

from trading_agents.llm import OllamaClient
from trading_agents.models import (
    Approval,
    BacktestSnapshot,
    DailyReviewSnapshot,
    EvaluationReport,
    MarketSnapshot,
    SentimentSnapshot,
    StrategyReflectionSnapshot,
    StrategyResearchSnapshot,
    TradeIdea,
)
from trading_agents.sentiment import SentimentDataProvider


def _order_flow_bias(snapshot: MarketSnapshot) -> float:
    return (
        float(getattr(snapshot, "top_book_imbalance", 0.0) or 0.0) * 0.25
        + float(getattr(snapshot, "depth_imbalance", 0.0) or 0.0) * 0.40
        + float(getattr(snapshot, "trade_delta_ratio", 0.0) or 0.0) * 0.35
    )


def _order_flow_summary(snapshot: MarketSnapshot) -> str:
    spread_bps = float(getattr(snapshot, "spread_bps", 0.0) or 0.0)
    depth_imbalance = float(getattr(snapshot, "depth_imbalance", 0.0) or 0.0)
    trade_delta_ratio = float(getattr(snapshot, "trade_delta_ratio", 0.0) or 0.0)
    large_buy_count = int(getattr(snapshot, "large_buy_count", 0) or 0)
    large_sell_count = int(getattr(snapshot, "large_sell_count", 0) or 0)
    bid_wall_notional = float(getattr(snapshot, "bid_wall_notional", 0.0) or 0.0)
    ask_wall_notional = float(getattr(snapshot, "ask_wall_notional", 0.0) or 0.0)
    bid_wall_distance = float(getattr(snapshot, "bid_wall_distance_bps", 0.0) or 0.0)
    ask_wall_distance = float(getattr(snapshot, "ask_wall_distance_bps", 0.0) or 0.0)
    levels = int(getattr(snapshot, "orderbook_levels", 0) or 0)
    trade_count = int(getattr(snapshot, "recent_trade_count", 0) or 0)
    if spread_bps <= 0 and levels <= 0 and trade_count <= 0:
        return "order flow unavailable"

    if depth_imbalance >= 0.18:
        book_bias = "bid-side depth dominates"
    elif depth_imbalance <= -0.18:
        book_bias = "ask-side depth dominates"
    else:
        book_bias = "depth fairly balanced"

    if trade_delta_ratio >= 0.18:
        tape_bias = "aggressive buyers in control"
    elif trade_delta_ratio <= -0.18:
        tape_bias = "aggressive sellers in control"
    else:
        tape_bias = "tape roughly two-way"

    if bid_wall_notional > ask_wall_notional * 1.2 and bid_wall_notional > 0:
        wall_bias = f"bid wall {bid_wall_distance:.1f}bps below"
    elif ask_wall_notional > bid_wall_notional * 1.2 and ask_wall_notional > 0:
        wall_bias = f"ask wall {ask_wall_distance:.1f}bps above"
    else:
        wall_bias = "no dominant wall"

    large_prints = ""
    if large_buy_count or large_sell_count:
        large_prints = f"; large prints buy={large_buy_count} sell={large_sell_count}"

    return (
        f"spread={spread_bps:.2f}bps; {book_bias}; "
        f"trade_delta={trade_delta_ratio:+.2f}; {tape_bias}; {wall_bias}{large_prints}"
    )


class MarketCollectorAgent:
    name = "market_collector"

    def summarize(self, snapshot: MarketSnapshot) -> str:
        avg_volume = fmean(snapshot.volumes) if snapshot.volumes else 0.0
        return (
            f"{snapshot.symbol} {snapshot.timeframe} "
            f"last={snapshot.last_price:.2f} "
            f"avg_volume={avg_volume:.2f} "
            f"candles={len(snapshot.closes)}"
        )


class OrderFlowCollectorAgent:
    name = "order_flow_collector"

    def summarize(self, snapshot: MarketSnapshot) -> str:
        return _order_flow_summary(snapshot)

    def bias_score(self, snapshot: MarketSnapshot) -> float:
        return _order_flow_bias(snapshot)


class SentimentCollectorAgent:
    name = "sentiment_collector"

    def __init__(self, provider: SentimentDataProvider | None = None) -> None:
        self.provider = provider or SentimentDataProvider()

    def summarize(self, symbol: str) -> SentimentSnapshot:
        return self.provider.collect(symbol).snapshot


class StrategistAgent:
    name = "strategist"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        sentiment: SentimentSnapshot,
        backtest: BacktestSnapshot,
        strategy_research: StrategyResearchSnapshot,
        market_summary: str,
        available_usdt: float,
        available_base_asset: float,
        position_side: str,
        min_order_value_usdt: float,
        aggressive_mode: bool,
        trading_mode: str,
        strategy_memory: dict | None = None,
        risk_feedback: str = "",
    ) -> TradeIdea:
        short_window = snapshot.closes[-5:]
        long_window = snapshot.closes[-20:]
        short_avg = fmean(short_window)
        long_avg = fmean(long_window)
        momentum = (short_avg - long_avg) / long_avg if long_avg else 0.0
        order_flow_summary = _order_flow_summary(snapshot)
        order_flow_bias = _order_flow_bias(snapshot)

        fallback = self._fallback_idea(
            momentum,
            sentiment,
            backtest,
            strategy_research,
            order_flow_bias,
            order_flow_summary,
            available_usdt,
            available_base_asset,
            position_side,
            snapshot.last_price,
            min_order_value_usdt,
            aggressive_mode,
            trading_mode,
        )
        if self.llm_client is None:
            return fallback
        memory_summary = self._strategy_memory_summary(strategy_memory)
        market_type = "perp" if "perp" in trading_mode else "spot"
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the strategist agent for a crypto trading system. "
                    "Return JSON with keys action, score, rationale, invalidation, holding_horizon. "
                    "Allowed action values: buy, sell, hold. "
                    "Score must be a decimal between 0 and 1. "
                    f"Symbol={snapshot.symbol}; timeframe={snapshot.timeframe}; "
                    f"market_summary={market_summary}; "
                    f"last_price={snapshot.last_price:.4f}; momentum={momentum:.6f}; "
                    f"order_flow_summary={order_flow_summary}; "
                    f"spread_bps={float(getattr(snapshot, 'spread_bps', 0.0) or 0.0):.2f}; "
                    f"top_book_imbalance={float(getattr(snapshot, 'top_book_imbalance', 0.0) or 0.0):+.4f}; "
                    f"depth_imbalance={float(getattr(snapshot, 'depth_imbalance', 0.0) or 0.0):+.4f}; "
                    f"trade_delta_ratio={float(getattr(snapshot, 'trade_delta_ratio', 0.0) or 0.0):+.4f}; "
                    f"large_buy_count={int(getattr(snapshot, 'large_buy_count', 0) or 0)}; "
                    f"large_sell_count={int(getattr(snapshot, 'large_sell_count', 0) or 0)}; "
                    f"sentiment_score={sentiment.sentiment_score:.2f}; "
                    f"sentiment_summary={sentiment.summary}; "
                    f"backtest_summary={backtest.summary}; "
                    f"strategy_research_summary={strategy_research.summary}; "
                    f"available_usdt={available_usdt:.2f}; "
                    f"available_base_asset={available_base_asset:.6f}; "
                    f"position_side={position_side}; "
                    f"selected_expectancy_pct={self._selected_strategy_backtest(strategy_research).expectancy_pct:+.4f}; "
                    f"selected_profit_factor={self._selected_strategy_backtest(strategy_research).profit_factor:.4f}; "
                    f"strategy_memory={memory_summary}; "
                    f"risk_feedback={risk_feedback}; "
                    f"aggressive_demo_mode={str(aggressive_mode).lower()}; "
                    f"trading_mode={trading_mode}; "
                    f"market_type={market_type}; "
                    "In spot mode, avoid proposing sell when there is no base asset. "
                    "In perp mode, buy means bullish intent (open long or close short) and sell means bearish intent (open short or close long). "
                    "If there is no available USDT, avoid opening new exposure. "
                    "In demo training mode, prefer positive-expectancy setups even when fear sentiment is elevated."
                )
            )
            action = str(response.get("action", fallback.action)).lower()
            if action not in {"buy", "sell", "hold"}:
                action = fallback.action
            idea = TradeIdea(
                action=action,
                score=self._coerce_score(response.get("score"), fallback.score),
                rationale=str(response.get("rationale", fallback.rationale)),
                invalidation=str(response.get("invalidation", fallback.invalidation)),
                holding_horizon=str(response.get("holding_horizon", fallback.holding_horizon)),
            )
            return self._align_with_account(
                idea,
                fallback,
                available_usdt,
                available_base_asset,
                position_side,
                trading_mode,
            )
        except Exception:
            return fallback

    def refine_with_risk_feedback(
        self,
        idea: TradeIdea,
        risk_feedback: str,
        available_usdt: float,
        available_base_asset: float,
        position_side: str,
        trading_mode: str,
        strategy_memory: dict | None = None,
    ) -> TradeIdea:
        if not risk_feedback.strip():
            return idea
        if self.llm_client is None:
            if "below minimum" in risk_feedback.lower():
                return TradeIdea(
                    action="hold",
                    score=min(idea.score, 0.45),
                    rationale=f"{idea.rationale}; revised to hold after risk critique: {risk_feedback}",
                    invalidation=idea.invalidation,
                    holding_horizon="none",
                )
            return idea
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the strategist revising a trade idea after risk critique. "
                    "Return JSON with keys action, score, rationale, invalidation, holding_horizon. "
                    "Allowed action values: buy, sell, hold. "
                    f"current_idea={idea.__dict__}; "
                    f"risk_feedback={risk_feedback}; "
                    f"available_usdt={available_usdt:.2f}; "
                    f"available_base_asset={available_base_asset:.6f}; "
                    f"position_side={position_side}; "
                    f"trading_mode={trading_mode}; "
                    f"strategy_memory={self._strategy_memory_summary(strategy_memory)}"
                )
            )
            revised = TradeIdea(
                action=str(response.get("action", idea.action)).lower(),
                score=self._coerce_score(response.get("score"), idea.score),
                rationale=str(response.get("rationale", idea.rationale)),
                invalidation=str(response.get("invalidation", idea.invalidation)),
                holding_horizon=str(response.get("holding_horizon", idea.holding_horizon)),
            )
            if revised.action not in {"buy", "sell", "hold"}:
                return idea
            return self._align_with_account(
                revised,
                idea,
                available_usdt,
                available_base_asset,
                position_side,
                trading_mode,
            )
        except Exception:
            return idea

    def _coerce_score(self, value: object, fallback: float) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return fallback
        if score > 1 and score <= 100:
            score = score / 100
        return max(0.0, min(score, 1.0))

    def _fallback_idea(
        self,
        momentum: float,
        sentiment: SentimentSnapshot,
        backtest: BacktestSnapshot,
        strategy_research: StrategyResearchSnapshot,
        order_flow_bias: float,
        order_flow_summary: str,
        available_usdt: float,
        available_base_asset: float,
        position_side: str,
        last_price: float,
        min_order_value_usdt: float,
        aggressive_mode: bool,
        trading_mode: str,
    ) -> TradeIdea:
        perp_mode = "perp" in trading_mode
        selected_backtest = self._selected_strategy_backtest(strategy_research)
        selected_is_base = strategy_research.selected_strategy_id == strategy_research.base_strategy_id
        backtest_supports_long = backtest.trade_count == 0 or (
            backtest.win_rate >= 0.45 and backtest.avg_return_pct >= -0.05
        )
        backtest_supports_short = backtest.trade_count == 0 or backtest.cumulative_return_pct >= -0.30
        if not selected_is_base and strategy_research.selected_strategy_id == "intraday_pullback_perp_v1":
            backtest_supports_long = backtest_supports_long and backtest.win_rate >= 0.40
        if not selected_is_base and strategy_research.selected_strategy_id == "intraday_breakout_perp_v1":
            backtest_supports_long = backtest_supports_long and backtest.trade_count >= 1

        sellable_notional = available_base_asset * last_price
        can_buy = available_usdt > 5
        if perp_mode:
            can_sell = position_side in {"flat", "long", "short"} and (
                position_side != "long"
                or min_order_value_usdt <= 0
                or sellable_notional >= min_order_value_usdt
            )
        else:
            can_sell = available_base_asset > 0 and (
                min_order_value_usdt <= 0 or sellable_notional >= min_order_value_usdt
            )
        cash_heavy = can_buy and (
            (perp_mode and position_side == "flat")
            or (not perp_mode and not can_sell)
        )
        selected_edge_positive = (
            selected_backtest.trade_count >= 2
            and selected_backtest.expectancy_pct >= (-0.01 if aggressive_mode else 0.0)
            and selected_backtest.profit_factor >= (0.95 if aggressive_mode else 1.05)
        )
        asymmetric_payoff = (
            selected_backtest.avg_win_pct > 0
            and abs(selected_backtest.avg_loss_pct) > 0
            and selected_backtest.avg_win_pct >= abs(selected_backtest.avg_loss_pct) * (1.15 if aggressive_mode else 1.40)
        )
        buy_sentiment_ok = sentiment.sentiment_score >= -0.35 or (
            aggressive_mode and selected_edge_positive and asymmetric_payoff
        )
        sell_sentiment_ok = sentiment.sentiment_score <= 0.60 or (
            aggressive_mode and selected_backtest.expectancy_pct >= -0.02
        )
        buy_flow_ok = order_flow_bias >= (-0.15 if aggressive_mode else -0.08)
        sell_flow_ok = order_flow_bias <= (0.15 if aggressive_mode else 0.08)
        flow_score_boost = min(max(abs(order_flow_bias), 0.0), 0.35) * 0.18
        current_signal = str(getattr(strategy_research, "current_signal", "hold") or "hold").lower()
        continuation_long_ready = (
            current_signal == "long"
            and momentum >= 0.0025
            and order_flow_bias >= 0.18
        )
        continuation_short_ready = (
            current_signal == "short"
            and momentum <= -0.0025
            and order_flow_bias <= -0.18
        )
        continuation_buy_sentiment_ok = buy_sentiment_ok or (
            continuation_long_ready and sentiment.sentiment_score >= -0.50
        )
        continuation_sell_sentiment_ok = sell_sentiment_ok or (
            continuation_short_ready and sentiment.sentiment_score <= 0.75
        )

        if momentum > (0.0010 if aggressive_mode else 0.0020) and buy_sentiment_ok and buy_flow_ok and (
            backtest_supports_long or selected_edge_positive
        ) and can_buy:
            return TradeIdea(
                action="buy",
                score=min((0.54 if aggressive_mode else 0.50) + momentum * 28 + flow_score_boost, 0.99),
                rationale=(
                    f"short MA above long MA by {momentum:.4%}; "
                    f"order_flow={order_flow_summary}; "
                    f"sentiment={sentiment.sentiment_score:+.2f}; "
                    f"backtest={backtest.summary}; "
                    f"strategy={strategy_research.selected_strategy_id}; "
                    f"expectancy={selected_backtest.expectancy_pct:+.2f}%; "
                    f"profit_factor={selected_backtest.profit_factor:.2f}"
                ),
                invalidation="exit if momentum weakens or sentiment flips sharply negative",
                holding_horizon="intraday",
            )
        if momentum < (-0.0010 if aggressive_mode else -0.0020) and sell_sentiment_ok and sell_flow_ok and (
            backtest_supports_short or selected_backtest.expectancy_pct >= -0.02
        ) and can_sell:
            return TradeIdea(
                action="sell",
                score=min((0.53 if aggressive_mode else 0.50) + abs(momentum) * 28 + flow_score_boost, 0.99),
                rationale=(
                    f"short MA below long MA by {abs(momentum):.4%}; "
                    f"order_flow={order_flow_summary}; "
                    f"sentiment={sentiment.sentiment_score:+.2f}; "
                    f"backtest={backtest.summary}; "
                    f"strategy={strategy_research.selected_strategy_id}; "
                    f"expectancy={selected_backtest.expectancy_pct:+.2f}%; "
                    f"profit_factor={selected_backtest.profit_factor:.2f}; "
                    f"position_side={position_side}"
                ),
                invalidation="exit if downward momentum fades",
                holding_horizon="intraday",
            )
        if current_signal == "short" and can_sell and continuation_sell_sentiment_ok and (
            selected_backtest.expectancy_pct >= (-0.02 if aggressive_mode else 0.0)
            or backtest_supports_short
        ):
            return TradeIdea(
                action="sell",
                score=min(0.61 + max(abs(momentum), 0.0) * 16 + flow_score_boost, 0.93),
                rationale=(
                    "external trend-following strategy still sees active bearish continuation; "
                    f"order_flow={order_flow_summary}; "
                    f"momentum={momentum:+.4%}; "
                    f"strategy={strategy_research.selected_strategy_id}; "
                    f"current_signal={current_signal}; "
                    f"expectancy={selected_backtest.expectancy_pct:+.2f}%; "
                    f"profit_factor={selected_backtest.profit_factor:.2f}; "
                    f"position_side={position_side}"
                ),
                invalidation="exit if continuation signal collapses or downward momentum fades",
                holding_horizon="intraday",
            )
        if current_signal == "long" and can_buy and continuation_buy_sentiment_ok and (
            selected_backtest.expectancy_pct >= (-0.02 if aggressive_mode else 0.0)
            or backtest_supports_long
        ):
            return TradeIdea(
                action="buy",
                score=min(0.61 + max(momentum, 0.0) * 16 + flow_score_boost, 0.93),
                rationale=(
                    "external trend-following strategy still sees active bullish continuation; "
                    f"order_flow={order_flow_summary}; "
                    f"momentum={momentum:+.4%}; "
                    f"strategy={strategy_research.selected_strategy_id}; "
                    f"current_signal={current_signal}; "
                    f"expectancy={selected_backtest.expectancy_pct:+.2f}%; "
                    f"profit_factor={selected_backtest.profit_factor:.2f}"
                ),
                invalidation="exit if continuation signal collapses or upside momentum fades",
                holding_horizon="intraday",
            )
        if (
            cash_heavy
            and selected_backtest.trade_count >= 3
            and selected_backtest.expectancy_pct >= (-0.02 if aggressive_mode else 0.02)
            and selected_backtest.profit_factor >= (0.95 if aggressive_mode else 1.10)
            and momentum > (-0.002 if aggressive_mode else -0.001)
            and order_flow_bias > (-0.10 if aggressive_mode else -0.04)
            and sentiment.sentiment_score >= -0.95
        ):
            confidence = min(
                0.59
                + max(momentum, 0.0) * 18
                + max(order_flow_bias, 0.0) * 0.16
                + min(max(selected_backtest.expectancy_pct, 0.0), 0.60) * 0.10
                + min(selected_backtest.cumulative_return_pct, 2.0) * 0.02,
                0.79,
            )
            return TradeIdea(
                action="buy",
                score=max(0.57, confidence),
                rationale=(
                    "cash-heavy account prefers executable long setup; "
                    f"momentum={momentum:+.4%}; "
                    f"order_flow={order_flow_summary}; "
                    f"selected_strategy={strategy_research.selected_strategy_id}; "
                    f"selected_replay={selected_backtest.summary}; "
                    f"sentiment={sentiment.sentiment_score:+.2f}"
                ),
                invalidation="exit if momentum turns negative or replay edge fades",
                holding_horizon="intraday",
            )
        if (
            cash_heavy
            and selected_backtest.trade_count >= 4
            and selected_backtest.expectancy_pct >= (-0.01 if aggressive_mode else 0.03)
            and selected_backtest.profit_factor >= (1.00 if aggressive_mode else 1.15)
            and (asymmetric_payoff or selected_backtest.win_rate >= 0.45)
            and backtest.expectancy_pct >= (-0.03 if aggressive_mode else 0.0)
            and sentiment.source_count >= 2
        ):
            confidence = min(
                0.58
                + min(max(selected_backtest.expectancy_pct, 0.0), 0.75) * 0.10
                + min(selected_backtest.cumulative_return_pct, 2.5) * 0.025,
                0.76,
            )
            return TradeIdea(
                action="buy",
                score=max(0.56, confidence),
                rationale=(
                    "cash-heavy account prefers a starter long when research replay is clearly stronger "
                    f"than staying idle; selected_strategy={strategy_research.selected_strategy_id}; "
                    f"order_flow={order_flow_summary}; "
                    f"selected_replay={selected_backtest.summary}; "
                    f"baseline_replay={backtest.summary}; "
                    f"sentiment={sentiment.sentiment_score:+.2f}"
                ),
                invalidation="exit if selected strategy edge weakens or price loses follow-through",
                holding_horizon="intraday",
            )
        return TradeIdea(
            action="hold",
            score=0.40,
            rationale=(
                "market momentum, sentiment, or replay confirmation is too weak; "
                f"account_usdt={available_usdt:.2f}; "
                f"account_base_asset={available_base_asset:.6f}; "
                f"backtest={backtest.summary}; "
                f"strategy={strategy_research.selected_strategy_id}; "
                f"selected_expectancy={selected_backtest.expectancy_pct:+.2f}%; "
                f"selected_profit_factor={selected_backtest.profit_factor:.2f}"
            ),
            invalidation="wait for stronger alignment between price and sentiment",
            holding_horizon="none",
        )

    def _align_with_account(
        self,
        idea: TradeIdea,
        fallback: TradeIdea,
        available_usdt: float,
        available_base_asset: float,
        position_side: str,
        trading_mode: str,
    ) -> TradeIdea:
        perp_mode = "perp" in trading_mode
        if not perp_mode and idea.action == "sell" and available_base_asset <= 0:
            if fallback.action != "sell":
                return fallback
            return TradeIdea(
                action="hold",
                score=min(idea.score, 0.45),
                rationale=(
                    f"{idea.rationale}; converted to hold because spot account has no base asset to sell"
                ),
                invalidation=idea.invalidation,
                holding_horizon="none",
            )
        opens_new_exposure = (
            (idea.action == "buy" and (not perp_mode or position_side != "short"))
            or (idea.action == "sell" and perp_mode and position_side != "long")
        )
        if opens_new_exposure and available_usdt <= 5:
            if fallback.action != idea.action:
                return fallback
            return TradeIdea(
                action="hold",
                score=min(idea.score, 0.45),
                rationale=f"{idea.rationale}; converted to hold because available USDT is too low",
                invalidation=idea.invalidation,
                holding_horizon="none",
            )
        if idea.action == "hold" and fallback.action == "buy" and available_base_asset <= 0 and available_usdt > 5:
            return fallback
        return idea

    def _selected_strategy_backtest(self, strategy_research: StrategyResearchSnapshot) -> BacktestSnapshot:
        for candidate in strategy_research.candidates:
            if candidate.strategy_id == strategy_research.selected_strategy_id:
                return cast(BacktestSnapshot, candidate.backtest)
        return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "selected strategy replay unavailable")

    def _strategy_memory_summary(self, strategy_memory: dict | None) -> str:
        strategy_memory = strategy_memory or {}
        return json.dumps(
            {
                "slot": strategy_memory.get("slot", ""),
                "summary": strategy_memory.get("summary", ""),
                "biases": strategy_memory.get("biases", []),
                "risk_adjustments": strategy_memory.get("risk_adjustments", []),
                "focus_symbols": strategy_memory.get("focus_symbols", []),
                "controls": strategy_memory.get("controls", {}),
            },
            ensure_ascii=False,
        )


class RiskSupervisorAgent:
    name = "risk_supervisor"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def review(
        self,
        idea: TradeIdea,
        sentiment: SentimentSnapshot,
        backtest: BacktestSnapshot,
        strategy_research: StrategyResearchSnapshot,
        available_usdt: float,
        available_base_asset: float,
        position_side: str,
        last_price: float,
        min_order_value_usdt: float,
        min_signal_score: float,
        max_position_pct: float,
        trading_mode: str,
        aggressive_mode: bool,
        expectancy_floor_pct: float,
        taker_fee_pct: float,
        buy_balance_buffer_pct: float,
        fee_hurdle_multiplier: float,
        cycle_mode: str,
        signal_boost: float = 0.0,
        strategy_memory: dict | None = None,
        use_llm: bool = True,
        total_equity_usdt: float = 0.0,
        current_position_notional_usdt: float = 0.0,
        current_leverage: float = 0.0,
        liq_price: float = 0.0,
        position_mm_usdt: float = 0.0,
        perp_max_leverage: float = 0.0,
        perp_min_available_balance_ratio_pct: float = 0.0,
        perp_min_liquidation_buffer_pct: float = 0.0,
    ) -> Approval:
        perp_mode = "perp" in trading_mode
        demo_mode = trading_mode.startswith("bybit-demo")
        selected_backtest = self._selected_strategy_backtest(strategy_research)
        buffered_available_usdt = max(available_usdt * buy_balance_buffer_pct, 0.0)
        opening_long = idea.action == "buy" and (not perp_mode or position_side != "short")
        opening_short = idea.action == "sell" and perp_mode and position_side != "long"
        closing_position = perp_mode and (
            (idea.action == "buy" and position_side == "short")
            or (idea.action == "sell" and position_side == "long")
        )
        effective_max_position_pct = max_position_pct
        if aggressive_mode and demo_mode:
            effective_max_position_pct = max(max_position_pct, 0.20)
        max_notional = buffered_available_usdt * effective_max_position_pct
        if (
            aggressive_mode
            and demo_mode
            and (idea.action == "buy" or opening_short)
            and min_order_value_usdt > 0
            and buffered_available_usdt >= min_order_value_usdt * 1.15
        ):
            # Keep at least one exchange-valid starter order available in demo mode
            # so small-but-viable setups do not get blocked by percentage sizing alone.
            max_notional = max(max_notional, min_order_value_usdt * 1.05)
        warnings: list[str] = []
        if idea.action == "hold":
            return Approval(False, "no trade proposed", 0.0, warnings)
        effective_min_signal = min_signal_score
        if aggressive_mode and demo_mode:
            effective_min_signal = min(min_signal_score, 0.52)
        effective_min_signal = min(max(effective_min_signal + signal_boost, 0.0), 0.99)
        selected_edge_positive = (
            selected_backtest.trade_count >= 2
            and selected_backtest.expectancy_pct >= expectancy_floor_pct
            and selected_backtest.profit_factor >= (0.95 if aggressive_mode else 1.05)
        )
        asymmetric_payoff = (
            selected_backtest.avg_win_pct > 0
            and abs(selected_backtest.avg_loss_pct) > 0
            and selected_backtest.avg_win_pct >= abs(selected_backtest.avg_loss_pct) * (1.10 if aggressive_mode else 1.35)
        )
        if idea.score < effective_min_signal and not (
            aggressive_mode and idea.action == "buy" and selected_edge_positive and asymmetric_payoff
        ):
            return Approval(False, f"signal score too low: {idea.score:.2f}", 0.0, warnings)
        effective_taker_fee_pct = taker_fee_pct
        effective_fee_hurdle_multiplier = fee_hurdle_multiplier
        if perp_mode and demo_mode:
            effective_taker_fee_pct = min(taker_fee_pct, 0.00055)
            effective_fee_hurdle_multiplier = min(fee_hurdle_multiplier, 1.0)
        round_trip_fee_pct = effective_taker_fee_pct * 200.0
        fee_hurdle_pct = round_trip_fee_pct * max(effective_fee_hurdle_multiplier, 0.0)
        if (
            idea.action in {"buy", "sell"}
            and selected_backtest.trade_count >= 2
            and selected_backtest.expectancy_pct < fee_hurdle_pct
        ):
            return Approval(
                False,
                f"expected edge below fee hurdle: {selected_backtest.expectancy_pct:.2f}% < {fee_hurdle_pct:.2f}%",
                0.0,
                warnings,
            )
        if sentiment.source_count < 2 and not aggressive_mode:
            return Approval(False, "not enough sentiment sources", 0.0, warnings)
        if (
            backtest.trade_count > 0
            and backtest.cumulative_return_pct < -0.50
            and selected_backtest.expectancy_pct < expectancy_floor_pct
        ):
            return Approval(False, "recent replay result is too weak", 0.0, warnings)
        if (
            strategy_research.selected_strategy_id != strategy_research.base_strategy_id
            and strategy_research.candidates
            and not any(item.backtest.trade_count > 0 for item in strategy_research.candidates)
        ):
            warnings.append("research strategy pool has too few recent replay samples")
        current_equity = max(total_equity_usdt, available_usdt, 0.0)
        current_exposure = abs(current_position_notional_usdt)
        if opening_long and buffered_available_usdt <= 5:
            return Approval(False, "not enough USDT to open a position", 0.0, warnings)
        if not perp_mode and idea.action == "sell" and available_base_asset <= 0:
            return Approval(False, "no base asset available to sell", 0.0, warnings)
        if idea.action == "sell" and min_order_value_usdt > 0 and (not perp_mode or position_side == "long"):
            sellable_notional = available_base_asset * last_price
            if sellable_notional < min_order_value_usdt:
                return Approval(
                    False,
                    f"position value below exchange minimum: {sellable_notional:.2f} < {min_order_value_usdt:.2f} USDT",
                    0.0,
                    warnings,
                )
            max_notional = sellable_notional
        if idea.action == "buy" and closing_position and min_order_value_usdt > 0:
            coverable_notional = available_base_asset * last_price
            if coverable_notional < min_order_value_usdt:
                return Approval(
                    False,
                    f"position value below exchange minimum: {coverable_notional:.2f} < {min_order_value_usdt:.2f} USDT",
                    0.0,
                    warnings,
                )
            max_notional = coverable_notional
        if abs(sentiment.sentiment_score) > 0.70:
            warnings.append("social sentiment is extreme; verify with more sources")
        if (opening_long or opening_short) and max_notional <= 0:
            return Approval(False, "no available balance", 0.0, warnings)
        if (opening_long or opening_short) and min_order_value_usdt > 0 and max_notional < min_order_value_usdt:
            return Approval(
                False,
                f"max position below exchange minimum: {max_notional:.2f} < {min_order_value_usdt:.2f} USDT",
                0.0,
                warnings,
            )
        if perp_mode and current_equity > 0:
            projected_exposure = current_exposure
            projected_available_balance = max(available_usdt, 0.0)
            if opening_long or opening_short:
                projected_exposure += max_notional
                projected_available_balance = max(projected_available_balance - max_notional, 0.0)
            elif closing_position:
                projected_exposure = max(current_exposure - max_notional, 0.0)
            effective_leverage = projected_exposure / current_equity if current_equity > 0 else 0.0
            if perp_max_leverage > 0 and effective_leverage > perp_max_leverage + 1e-9:
                return Approval(
                    False,
                    f"projected leverage too high: {effective_leverage:.2f}x > {perp_max_leverage:.2f}x",
                    0.0,
                    warnings,
                )
            available_balance_ratio_pct = (projected_available_balance / current_equity * 100.0) if current_equity > 0 else 0.0
            if (
                (opening_long or opening_short)
                and perp_min_available_balance_ratio_pct > 0
                and available_balance_ratio_pct < perp_min_available_balance_ratio_pct
            ):
                return Approval(
                    False,
                    (
                        "projected available balance too low: "
                        f"{available_balance_ratio_pct:.1f}% < {perp_min_available_balance_ratio_pct:.1f}% of equity"
                    ),
                    0.0,
                    warnings,
                )
            if liq_price > 0 and last_price > 0 and current_exposure > 0:
                liq_buffer_pct = abs((last_price - liq_price) / last_price) * 100.0
                if opening_long or opening_short:
                    if perp_min_liquidation_buffer_pct > 0 and liq_buffer_pct < perp_min_liquidation_buffer_pct:
                        return Approval(
                            False,
                            f"liquidation buffer too tight: {liq_buffer_pct:.2f}% < {perp_min_liquidation_buffer_pct:.2f}%",
                            0.0,
                            warnings,
                        )
                elif liq_buffer_pct < max(perp_min_liquidation_buffer_pct * 1.25, perp_min_liquidation_buffer_pct):
                    warnings.append(f"liquidation buffer is tight: {liq_buffer_pct:.2f}%")
            if position_mm_usdt > 0 and current_equity > 0:
                mm_ratio_pct = position_mm_usdt / current_equity * 100.0
                if mm_ratio_pct >= 35.0:
                    warnings.append(f"maintenance margin elevated: {mm_ratio_pct:.1f}% of equity")
        if cycle_mode == "fast" and idea.action in {"buy", "sell"} and idea.score < max(effective_min_signal, 0.64):
            return Approval(False, f"fast-cycle confidence too low: {idea.score:.2f}", 0.0, warnings)
        if idea.action == "sell" and available_base_asset > 0 and (not perp_mode or position_side == "long"):
            max_notional = max(max_notional, 1.0)
        if idea.action == "buy" and perp_mode and position_side == "short" and available_base_asset > 0:
            max_notional = max(max_notional, 1.0)
        if self.llm_client is not None and use_llm:
            try:
                response = self.llm_client.generate_json(
                    (
                        "You are the risk supervisor for a crypto trading system. "
                        "Return JSON with keys approved, reason, warnings. "
                        "Be conservative, but do not reject solely because sentiment is negative if the trade already aligns with risk rules. "
                        "In demo training mode, allow positive-expectancy setups with controlled size even if confidence is only moderate. "
                        f"idea_action={idea.action}; idea_score={idea.score:.2f}; "
                        f"sentiment_score={sentiment.sentiment_score:.2f}; "
                        f"backtest_summary={backtest.summary}; "
                        f"strategy_research_summary={strategy_research.summary}; "
                        f"available_usdt={available_usdt:.2f}; available_base_asset={available_base_asset:.6f}; "
                        f"position_side={position_side}; "
                        f"max_position_pct={effective_max_position_pct:.2f}; "
                        f"cycle_mode={cycle_mode}; "
                        f"selected_expectancy_pct={selected_backtest.expectancy_pct:+.4f}; "
                        f"selected_profit_factor={selected_backtest.profit_factor:.4f}; "
                        f"strategy_memory={json.dumps(strategy_memory or {}, ensure_ascii=False)}; "
                        f"aggressive_demo_mode={str(aggressive_mode).lower()}; "
                        f"trading_mode={trading_mode}"
                    )
                )
                llm_approved = bool(response.get("approved", True))
                llm_reason = str(response.get("reason", "risk checks passed"))
                llm_warnings = response.get("warnings", [])
                if isinstance(llm_warnings, list):
                    warnings.extend(str(item) for item in llm_warnings)
                if not llm_approved:
                    if aggressive_mode and demo_mode and selected_edge_positive:
                        warnings.append(f"risk llm caution: {llm_reason}")
                    else:
                        return Approval(False, llm_reason, 0.0, warnings)
            except Exception:
                pass
        return Approval(True, "risk checks passed", max_notional, warnings)

    def critique(
        self,
        idea: TradeIdea,
        sentiment: SentimentSnapshot,
        backtest: BacktestSnapshot,
        strategy_research: StrategyResearchSnapshot,
        strategy_memory: dict | None = None,
        use_llm: bool = True,
    ) -> str:
        selected_backtest = self._selected_strategy_backtest(strategy_research)
        fallback_reasons: list[str] = []
        if idea.action != "hold" and selected_backtest.expectancy_pct < 0:
            fallback_reasons.append("selected strategy expectancy is negative")
        if idea.action == "buy" and sentiment.sentiment_score <= -0.75:
            fallback_reasons.append("fear sentiment is still extreme, so timing may need confirmation")
        if idea.action == "sell" and backtest.trade_count > 0 and backtest.expectancy_pct > 0.03:
            fallback_reasons.append("baseline replay is still net positive, so exit may be too early")
        fallback = "; ".join(fallback_reasons)
        if self.llm_client is None or not use_llm:
            return fallback
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the risk supervisor providing a short critique before final approval. "
                    "Return JSON with keys concern and severity. "
                    "If there is no meaningful concern, return an empty concern. "
                    f"idea={idea.__dict__}; "
                    f"sentiment_summary={sentiment.summary}; "
                    f"backtest_summary={backtest.summary}; "
                    f"strategy_research_summary={strategy_research.summary}; "
                    f"strategy_memory={json.dumps(strategy_memory or {}, ensure_ascii=False)}"
                )
            )
            concern = str(response.get("concern", "")).strip()
            return concern or fallback
        except Exception:
            return fallback

    def _selected_strategy_backtest(self, strategy_research: StrategyResearchSnapshot) -> BacktestSnapshot:
        for candidate in strategy_research.candidates:
            if candidate.strategy_id == strategy_research.selected_strategy_id:
                return cast(BacktestSnapshot, candidate.backtest)
        return BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "selected strategy replay unavailable")


class ExecutorAgent:
    name = "executor"

    def build_order(
        self,
        symbol: str,
        side: str,
        notional_usdt: float,
        price: float,
        available_usdt: float,
        available_base_asset: float,
        trading_mode: str,
        position_side: str = "flat",
        buy_balance_buffer_pct: float = 1.0,
        target_leverage: float = 0.0,
    ) -> dict:
        perp_mode = "perp" in trading_mode
        if not perp_mode and side == "buy":
            capped_notional = min(notional_usdt, max(available_usdt * buy_balance_buffer_pct, 0.0))
            quantity = capped_notional / price if price else 0.0
        elif not perp_mode:
            max_sell_notional = available_base_asset * price if price else 0.0
            capped_notional = min(notional_usdt, max_sell_notional)
            quantity = min(available_base_asset, capped_notional / price) if price else 0.0
        else:
            reducing_position = (side == "buy" and position_side == "short") or (side == "sell" and position_side == "long")
            if reducing_position:
                max_reducible_notional = available_base_asset * price if price else 0.0
                capped_notional = min(notional_usdt, max_reducible_notional)
                quantity = min(available_base_asset, capped_notional / price) if price else 0.0
            else:
                capped_notional = min(notional_usdt, max(available_usdt * buy_balance_buffer_pct, 0.0))
                quantity = capped_notional / price if price else 0.0
        return {
            "symbol": symbol,
            "side": side,
            "notional_usdt": round(capped_notional, 2),
            "quantity": round(quantity, 6),
            "price": round(price, 4),
            "reduce_only": bool(perp_mode and ((side == "buy" and position_side == "short") or (side == "sell" and position_side == "long"))),
            "target_leverage": round(float(target_leverage), 4) if target_leverage > 0 else 0.0,
        }


class PostTradeEvaluatorAgent:
    name = "post_trade_evaluator"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(self, idea: TradeIdea, result: dict) -> EvaluationReport:
        status = result.get("status", "unknown")
        fallback = self._fallback_evaluation(idea, result)
        if self.llm_client is None:
            return fallback
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the post-trade evaluator in a crypto trading system. "
                    "Return JSON with keys grade and notes. "
                    "Grades allowed: A, B, C, D. "
                    f"idea_action={idea.action}; idea_score={idea.score:.2f}; "
                    f"idea_invalidation={idea.invalidation}; result_status={status}; "
                    f"result={result}"
                )
            )
            grade = str(response.get("grade", fallback.grade)).upper()
            if grade not in {"A", "B", "C", "D"}:
                grade = fallback.grade
            notes = str(response.get("notes", fallback.notes))
            return EvaluationReport(grade=grade, notes=notes)
        except Exception:
            return fallback

    def _fallback_evaluation(self, idea: TradeIdea, result: dict) -> EvaluationReport:
        status = result.get("status", "unknown")
        if status == "filled":
            return EvaluationReport(
                grade="B",
                notes=(
                    f"trade executed successfully; next step is to compare outcome against "
                    f"invalidation rule: {idea.invalidation}"
                ),
            )
        if status == "accepted":
            return EvaluationReport(
                grade="B",
                notes="order was accepted by the exchange; confirm eventual fill state against execution logs",
            )
        return EvaluationReport(grade="C", notes="trade was not filled cleanly; inspect execution logs")


class DailyReviewAgent:
    name = "daily_reviewer"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(self, date_label: str, daily_summary: dict) -> DailyReviewSnapshot:
        fallback = self._fallback_review(date_label, daily_summary)
        if self.llm_client is None:
            return fallback
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the daily reviewer agent for a crypto trading system. "
                    "Return JSON with keys title, operations_summary, decision_summary, strategist_review, "
                    "risk_review, benchmark_review, execution_review, consensus_summary, "
                    "improvement_directions, action_items. "
                    "improvement_directions and action_items must be short arrays of concrete next steps. "
                    f"date_label={date_label}; "
                    f"daily_summary={json.dumps(daily_summary, ensure_ascii=False)}"
                )
            )
            directions = response.get("improvement_directions", fallback.improvement_directions)
            if not isinstance(directions, list):
                directions = fallback.improvement_directions
            normalized_directions = [str(item).strip() for item in directions if str(item).strip()]
            if not normalized_directions:
                normalized_directions = fallback.improvement_directions
            action_items = response.get("action_items", fallback.action_items)
            if not isinstance(action_items, list):
                action_items = fallback.action_items
            normalized_actions = [str(item).strip() for item in action_items if str(item).strip()]
            if not normalized_actions:
                normalized_actions = fallback.action_items
            return DailyReviewSnapshot(
                title=str(response.get("title", fallback.title)).strip() or fallback.title,
                operations_summary=str(response.get("operations_summary", fallback.operations_summary)).strip()
                or fallback.operations_summary,
                decision_summary=str(response.get("decision_summary", fallback.decision_summary)).strip()
                or fallback.decision_summary,
                improvement_directions=normalized_directions[:5],
                strategist_review=str(response.get("strategist_review", fallback.strategist_review)).strip() or fallback.strategist_review,
                risk_review=str(response.get("risk_review", fallback.risk_review)).strip() or fallback.risk_review,
                benchmark_review=str(response.get("benchmark_review", fallback.benchmark_review)).strip() or fallback.benchmark_review,
                execution_review=str(response.get("execution_review", fallback.execution_review)).strip() or fallback.execution_review,
                consensus_summary=str(response.get("consensus_summary", fallback.consensus_summary)).strip() or fallback.consensus_summary,
                action_items=normalized_actions[:5],
            )
        except Exception:
            return fallback

    def _fallback_review(self, date_label: str, daily_summary: dict) -> DailyReviewSnapshot:
        action_counts = daily_summary.get("action_counts", {})
        selected_symbol_counts = daily_summary.get("selected_symbol_counts", {})
        blocked_reason_counts = daily_summary.get("blocked_reason_counts", {})
        rejection_reason_counts = daily_summary.get("rejection_reason_counts", {})
        financial = daily_summary.get("financial_snapshot", {})
        latest = daily_summary.get("latest") or {}
        external_benchmarks = daily_summary.get("external_benchmarks", {})
        symbol_postmortem = daily_summary.get("symbol_postmortem") or {}
        loss_attribution = daily_summary.get("loss_attribution") or {}
        policy_exit_diagnostics = daily_summary.get("policy_exit_diagnostics") or {}
        strategy_memory = daily_summary.get("latest", {}).get("strategy_memory") or {}
        learning_controls = strategy_memory.get("controls") or {}
        action_line = ", ".join(f"{key}={value}" for key, value in action_counts.items()) or "no actions"
        symbol_line = ", ".join(f"{key}={value}" for key, value in selected_symbol_counts.items()) or "no symbol focus"
        top_block = next(iter(blocked_reason_counts.items()), ("none", 0))
        projected_balance_blocked_while_exposed = int(daily_summary.get("projected_balance_blocked_while_exposed", 0) or 0)
        projected_balance_blocked_while_flat = int(daily_summary.get("projected_balance_blocked_while_flat", 0) or 0)
        top_reject = next(iter(rejection_reason_counts.items()), ("none", 0))
        top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
        top_alpha = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]
        focus_symbol = str(symbol_postmortem.get("symbol", "") or "").strip()
        benchmark_by_symbol = ((external_benchmarks.get("top_by_symbol") or {}) if isinstance(external_benchmarks.get("top_by_symbol"), dict) else {})
        focus_benchmark = benchmark_by_symbol.get(focus_symbol) if focus_symbol else {}
        if not isinstance(focus_benchmark, dict):
            focus_benchmark = {}
        review_history = daily_summary.get("review_history") or []

        operations_summary = (
            f"目前總資產約 {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT，"
            f"單日損益 {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
            f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)，"
            f"資金利用率 {float(financial.get('capital_utilization_pct', 0.0)):.1f}%。"
            f"截至目前共有 {daily_summary.get('total', 0)} 次決策，"
            f"{daily_summary.get('submitted_orders', 0)} 次送單，"
            f"{daily_summary.get('accepted_orders', 0)} 次被交易所接受，"
            f"{daily_summary.get('rejected_orders', 0)} 次被交易所拒絕。"
            f"主要動作分布為 {action_line}；主要觀察/選中標的是 {symbol_line}。"
        )
        decision_summary = (
            f"最新決策聚焦在 {latest.get('selected_symbol', 'n/a')}，"
            f"訊號為 {latest.get('idea', {}).get('action', 'n/a')} "
            f"({float(latest.get('idea', {}).get('score', 0.0)):.2f})。"
            f"已實現損益 {float(financial.get('realized_pnl_usdt', 0.0)):+.2f} USDT，"
            f"未實現損益 {float(financial.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT。"
            f"主要 blocked 原因是 {top_block[0]} ({top_block[1]})；"
            f"主要 rejected 原因是 {top_reject[0]} ({top_reject[1]})。"
        )
        if symbol_postmortem.get("summary"):
            decision_summary += f" 單一標的檢討：{symbol_postmortem.get('summary')}"
        benchmark_for_review = focus_benchmark if focus_benchmark.get("candidate_id") else top_benchmark
        shadow_watch = daily_summary.get("shadow_benchmark_watch") or {}
        if benchmark_for_review.get("candidate_id"):
            benchmark_expectancy = float(benchmark_for_review.get("expectancy_pct", 0.0) or 0.0)
            benchmark_profit_factor = float(benchmark_for_review.get("profit_factor", 0.0) or 0.0)
            if benchmark_expectancy > 0.0 and benchmark_profit_factor > 1.0:
                decision_summary += (
                    f" 最新外部 benchmark 目前以 {benchmark_for_review.get('candidate_id', 'n/a')} "
                    f"@ {benchmark_for_review.get('symbol', 'n/a')} 領先，"
                    f"expectancy {benchmark_expectancy:+.2f}%。"
                )
            else:
                decision_summary += (
                    f" 最新外部 benchmark 雖然相對領先的是 {benchmark_for_review.get('candidate_id', 'n/a')} "
                    f"@ {benchmark_for_review.get('symbol', 'n/a')}，但扣完成本後 expectancy {benchmark_expectancy:+.2f}% / "
                    f"PF {benchmark_profit_factor:.2f}，仍未達正 edge。"
                )
        if top_alpha.get("candidate_id"):
            decision_summary += (
                f" Alpha Arena 對照組領先的是 {top_alpha.get('candidate_id', 'n/a')}。"
            )

        improvements: list[str] = []
        action_items: list[str] = []
        accepted_orders = int(daily_summary.get("accepted_orders", 0) or 0)
        daily_fees_usdt = float(financial.get("daily_fees_usdt", 0.0) or 0.0)
        realized_pnl_usdt = float(financial.get("realized_pnl_usdt", 0.0) or 0.0)
        unrealized_pnl_usdt = float(financial.get("unrealized_pnl_usdt", 0.0) or 0.0)
        if float(financial.get("daily_fees_usdt", 0.0)) > max(float(financial.get("daily_pnl_usdt", 0.0)), 0.0):
            improvements.append("手續費已接近或超過當日獲利，優先降低過度交易與低品質微型訊號。")
        if accepted_orders > 0 and daily_fees_usdt > max(realized_pnl_usdt, 0.0):
            improvements.append("已實現毛利不足以覆蓋手續費時，優先檢查止盈是否過遠，以及第一段 profit-lock 是否低於來回費用門檻。")
            action_items.append("回看當日交易：若浮盈常落在 +0.8%~+1.8% 後回吐，優先收緊 TP 與第一段鎖利。")
        if int(loss_attribution.get("carry_in_closed_count", 0) or 0) > 0 and float(financial.get("daily_pnl_usdt", 0.0) or 0.0) < 0:
            improvements.append("carry-in 倉位在最近窗口持續拖累績效時，應優先縮短持有容忍度，而不是只在事後用 stagnation exit 收尾。")
            action_items.append("檢查 carry-in 倉位是否反覆以 stagnation exit 收掉；若是，應維持更短的 hold/stagnation 規則直到 carry-in 不再主導虧損。")
        if unrealized_pnl_usdt > max(realized_pnl_usdt, 0.0) * 2 and daily_fees_usdt > 0:
            improvements.append("若帳面浮盈明顯大於已實現獲利，代表趨勢有抓到，但落袋節奏仍偏慢。")
        if daily_summary.get("rejected_orders", 0) > 0:
            improvements.append("在 executor 前補一層交易所最小單與最終下單 notional 檢查，避免把 rejected 當成有效成交。")
        if top_block[0] != "none" and top_block[1] > 0:
            if top_block[0] == "projected available balance too low of equity" and projected_balance_blocked_while_exposed > projected_balance_blocked_while_flat:
                improvements.append(
                    "多數 `projected available balance too low of equity` 發生在已有曝險倉位時；優先檢查加碼節奏與保證金預留是否過度保守，而不是把它當成空倉算式錯誤。"
                )
            else:
                improvements.append(f"優先處理 `{top_block[0]}`，降低可執行候選被風控或交易所門檻擋下的比例。")
        focused_symbols = [symbol for symbol, count in selected_symbol_counts.items() if int(count) > 0]
        if len(focused_symbols) == 1 and sum(selected_symbol_counts.values()) > 20:
            improvements.append("目前是單一標的專注模式，優先檢查同一標的上的進場品質、續抱節奏與再進場能力，而不是增加分散與輪動。")
        elif len(selected_symbol_counts) <= 2 and sum(selected_symbol_counts.values()) > 20:
            improvements.append("檢查 selector 是否過度集中在單一標的，必要時加入更明確的分散與輪動規則。")
        if daily_summary.get("accepted_orders", 0) == 0:
            improvements.append("盤中已有訊號但沒有實際成交時，優先檢查 sizing、最小單額與資金切分邏輯。")
        if float(financial.get("capital_utilization_pct", 0.0)) < 20:
            improvements.append("資金利用率偏低時，優先維持主策略門檻；若要增加 demo 訓練樣本，應額外設計獨立的 exploration budget，而不是直接降低整體進場標準。")
        if top_benchmark.get("candidate_id") and top_benchmark.get("candidate_id") != "donchian_adx_perp_v1":
            improvements.append(
                f"外部 benchmark 顯示 `{top_benchmark.get('candidate_id')}` 在 {top_benchmark.get('symbol', 'n/a')} 暫時更強，先做 attribution 再決定是否升級成 live 候選。"
            )
        if top_alpha.get("candidate_id"):
            improvements.append(
                "將 Alpha Arena 領先模型的持倉節奏與我們的 exit timing 對照，優先檢查出場是否過慢。"
            )
        for item in symbol_postmortem.get("improvement_directions", [])[:2]:
            if item not in improvements:
                improvements.append(str(item))
        if not improvements:
            improvements.append("持續追蹤各策略的 expectancy 與實際填單結果，讓 selector 更偏向真正可成交且報酬風險比佳的候選。")

        strategist_review = (
            f"策略面來看，今日主要由 {loss_attribution.get('primary_driver', 'n/a')} 主導，"
            f"動作分布為 {action_line}。"
            f" {symbol_postmortem.get('summary', '')}".strip()
        )
        if not strategist_review.strip():
            strategist_review = "策略面目前沒有足夠資料形成明確結論。"

        risk_review = (
            f"風控面最常擋下的是 {top_block[0]} ({top_block[1]})，"
            f"主要 rejected 原因是 {top_reject[0]} ({top_reject[1]})。"
            f" Policy exit 摘要：{policy_exit_diagnostics.get('summary', 'n/a')}。"
        )
        if top_block[0] == "projected available balance too low of equity" and top_block[1] > 0:
            risk_review += (
                f" 其中已有曝險倉位時被擋 {projected_balance_blocked_while_exposed} 次，"
                f"空倉時被擋 {projected_balance_blocked_while_flat} 次。"
            )

        benchmark_review = "目前沒有可用的外部 benchmark。"
        if benchmark_for_review.get("candidate_id"):
            benchmark_review = (
                f"外部 benchmark 顯示 {benchmark_for_review.get('candidate_id')} "
                f"在 {benchmark_for_review.get('symbol', 'n/a')} 暫時領先，"
                f"expectancy {float(benchmark_for_review.get('expectancy_pct', 0.0)):+.2f}% / "
                f"profit factor {float(benchmark_for_review.get('profit_factor', 0.0)):.2f}。"
            )
        if shadow_watch.get("status") == "ready":
            benchmark_review += (
                f" Shadow 對照中，`{shadow_watch.get('watch_candidate_id', 'n/a')}` 相對 "
                f"`{shadow_watch.get('baseline_candidate_id', 'n/a')}` 的 expectancy 差值為 "
                f"{float(shadow_watch.get('expectancy_delta_pct', 0.0)):+.2f}% 、"
                f"profit factor 差值為 {float(shadow_watch.get('profit_factor_delta', 0.0)):+.2f}，"
                f"目前判定為 `{shadow_watch.get('verdict', 'n/a')}`。"
            )

        execution_review = (
            f"執行面共有 {daily_summary.get('submitted_orders', 0)} 次送單、"
            f"{daily_summary.get('accepted_orders', 0)} 次接受、"
            f"{daily_summary.get('rejected_orders', 0)} 次拒絕。"
            f" 已接受交易來源分布為 "
            f"{' | '.join(f'{k}={int(v)}' for k, v in (loss_attribution.get('accepted_source_counts') or {}).items()) or 'none'}。"
        )

        if loss_attribution.get("primary_driver"):
            action_items.append(f"明天優先驗證 `{loss_attribution.get('primary_driver')}` 是否仍然主導績效。")
        if top_block[0] != "none" and top_block[1] > 0:
            action_items.append(f"針對 `{top_block[0]}` 做下一輪條件調整與複盤。")
        if benchmark_for_review.get("candidate_id"):
            action_items.append(
                f"將 `{benchmark_for_review.get('candidate_id')}` 與 live baseline 做同標的 attribution 對照。"
            )
        if shadow_watch.get("status") == "ready":
            action_items.append(str(shadow_watch.get("next_step", "")).strip())
        if learning_controls:
            action_items.append(f"確認 learning controls 是否真的落地：{json.dumps(learning_controls, ensure_ascii=False)}")
            if str(learning_controls.get("carry_in_mode", "") or "").strip().lower() == "de_risk":
                action_items.append("目前已啟用 carry-in de-risk；明天優先驗證提早退場是否有減少 carry-in 對損益的拖累。")
        if not action_items:
            action_items.append("繼續追蹤基準策略、風控、exit 與 benchmark 的責任歸屬。")

        improvements = self._promote_repeated_review_items(improvements, review_history)
        action_items = self._promote_repeated_action_items(action_items, review_history)

        consensus_summary = (
            f"綜合策略、風控、benchmark 與執行四個角度，"
            f"目前最值得追的不是新增更多策略，而是確認 {top_block[0] if top_block[0] != 'none' else '進場/出場品質'} "
            f"是否持續拖累績效，並驗證 benchmark 是否值得升級成更正式的候選。"
        )

        return DailyReviewSnapshot(
            title=f"Trading Agents Daily Review - {date_label}",
            operations_summary=operations_summary,
            decision_summary=decision_summary,
            improvement_directions=improvements[:4],
            strategist_review=strategist_review,
            risk_review=risk_review,
            benchmark_review=benchmark_review,
            execution_review=execution_review,
            consensus_summary=consensus_summary,
            action_items=action_items[:4],
        )

    def _promote_repeated_review_items(self, items: list[str], review_history: list[dict]) -> list[str]:
        promoted: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            streak = self._history_repeat_count(review_history, normalized, field="improvement_directions")
            if streak >= 2:
                promoted.append(
                    f"此問題已連續 {streak + 1} 天出現：{normalized} 這代表它不是單日噪音，下一步應升級成明確參數/策略調整，而不是只持續觀察。"
                )
            else:
                promoted.append(normalized)
        return promoted

    def _promote_repeated_action_items(self, items: list[str], review_history: list[dict]) -> list[str]:
        promoted: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            streak = self._history_repeat_count(review_history, normalized, field="action_items")
            if streak >= 2:
                promoted.append(self._escalate_repeated_action_item(normalized, streak + 1))
            else:
                promoted.append(normalized)
        return promoted

    def _history_repeat_count(self, review_history: list[dict], text: str, *, field: str) -> int:
        target = str(text).strip()
        if not target:
            return 0
        count = 0
        for review in reversed(review_history):
            values = review.get(field) or []
            normalized_values = [str(item).strip() for item in values if str(item).strip()]
            if target in normalized_values:
                count += 1
            else:
                break
        return count

    def _escalate_repeated_action_item(self, item: str, streak_days: int) -> str:
        if "fees outweighed realized trading edge" in item:
            return (
                f"`fees outweighed realized trading edge` 已連續 {streak_days} 天出現；"
                "下一步不要只驗證，直接比較「減少同 episode entries」與「更早鎖利」對淨利的影響。"
            )
        if "grid_range_reversion_v1" in item and "attribution" in item:
            return (
                f"`grid_range_reversion_v1` 已連續 {streak_days} 天被點名；"
                "下一步應建立同標的 shadow-vs-live 對照與明確升級門檻，而不是只持續追蹤。"
            )
        if "learning controls" in item:
            return (
                f"learning controls 已連續 {streak_days} 天需要人工確認；"
                "下一步應在報表直接顯示哪些 controls 真的影響了 accepted / blocked / PnL，而不是只提醒檢查。"
            )
        return (
            f"這個 action item 已連續 {streak_days} 天重複：{item} "
            "下一步應把它升級成更具體的參數變更、shadow test，或明確的升級/淘汰判準。"
        )


class StrategyReflectionAgent:
    name = "strategy_reflector"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(self, slot: str, daily_summary: dict, reflection_context: dict | None = None) -> StrategyReflectionSnapshot:
        fallback = self._fallback(slot, daily_summary, reflection_context=reflection_context)
        if self.llm_client is None:
            return fallback
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the strategy reflection agent for a crypto trading system. "
                    "This reflection runs only once every 12 hours to avoid overfitting. "
                    "Return JSON with keys summary, biases, risk_adjustments, focus_symbols, controls. "
                    "controls may include fallback_entry_mode (normal/base_only), entry_mode (normal/capital_preservation), cooldown_scale (0.25-1.0), "
                    "and benchmark_watch_candidate / benchmark_watch_symbol. "
                    f"slot={slot}; daily_summary={json.dumps(daily_summary, ensure_ascii=False)}; "
                    f"reflection_context={json.dumps(reflection_context or {}, ensure_ascii=False)}"
                )
            )
            biases = response.get("biases", fallback.biases)
            adjustments = response.get("risk_adjustments", fallback.risk_adjustments)
            focus_symbols = response.get("focus_symbols", fallback.focus_symbols)
            controls = response.get("controls", fallback.controls)
            if not isinstance(biases, list):
                biases = fallback.biases
            if not isinstance(adjustments, list):
                adjustments = fallback.risk_adjustments
            if not isinstance(focus_symbols, list):
                focus_symbols = fallback.focus_symbols
            if not isinstance(controls, dict):
                controls = fallback.controls
            return StrategyReflectionSnapshot(
                slot=slot,
                summary=str(response.get("summary", fallback.summary)).strip() or fallback.summary,
                biases=[str(item).strip() for item in biases if str(item).strip()][:4] or fallback.biases,
                risk_adjustments=[str(item).strip() for item in adjustments if str(item).strip()][:4] or fallback.risk_adjustments,
                focus_symbols=[str(item).strip() for item in focus_symbols if str(item).strip()][:4] or fallback.focus_symbols,
                controls=self._normalize_controls(controls, fallback.controls, reflection_context=reflection_context),
            )
        except Exception:
            return fallback

    def _fallback(self, slot: str, daily_summary: dict, reflection_context: dict | None = None) -> StrategyReflectionSnapshot:
        reflection_context = reflection_context or {}
        blocked = daily_summary.get("blocked_reason_counts", {})
        rejected = daily_summary.get("rejection_reason_counts", {})
        selected = daily_summary.get("selected_symbol_counts", {})
        financial = daily_summary.get("financial_snapshot", {})
        accepted_sources = daily_summary.get("accepted_source_counts", {})
        top_block = next(iter(blocked.items()), ("none", 0))
        top_reject = next(iter(rejected.items()), ("none", 0))
        external_benchmarks = daily_summary.get("external_benchmarks", {})
        top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
        live_symbols = [str(item).strip() for item in reflection_context.get("live_symbols", []) if str(item).strip()]
        focus_symbols = live_symbols or [key for key, _ in list(selected.items())[:3]]
        biases: list[str] = []
        risk_adjustments: list[str] = []
        controls: dict[str, object] = {}
        daily_pnl_usdt = float(financial.get("daily_pnl_usdt", 0.0) or 0.0)
        realized_pnl_usdt = float(financial.get("realized_pnl_usdt", 0.0) or 0.0)
        unrealized_pnl_usdt = float(financial.get("unrealized_pnl_usdt", 0.0) or 0.0)
        daily_fees_usdt = float(financial.get("daily_fees_usdt", 0.0) or 0.0)
        accepted_orders = int(daily_summary.get("accepted_orders", 0) or 0)
        fallback_accepted = int(accepted_sources.get("fallback", 0) or 0)
        base_accepted = int(accepted_sources.get("base_strategy", 0) or 0)
        cooldown_blocks = int(blocked.get("symbol cooldown active", 0) or 0)
        total_blocked = int(daily_summary.get("blocked", 0) or 0)
        lookback_days = int(reflection_context.get("lookback_days", 0) or 0)
        negative_day_count = int(reflection_context.get("negative_day_count", 0) or 0)
        negative_streak = int(reflection_context.get("negative_streak", 0) or 0)
        positive_streak = int(reflection_context.get("positive_streak", 0) or 0)
        carry_in_loss_window_count = int(reflection_context.get("carry_in_loss_window_count", 0) or 0)
        carry_in_loss_streak = int(reflection_context.get("carry_in_loss_streak", 0) or 0)
        stagnation_exit_window_count = int(reflection_context.get("stagnation_exit_window_count", 0) or 0)
        stagnation_exit_streak = int(reflection_context.get("stagnation_exit_streak", 0) or 0)
        repeated_benchmark_leader_id = str(reflection_context.get("repeated_benchmark_leader_id", "") or "").strip()
        benchmark_leader_streak = int(reflection_context.get("benchmark_leader_streak", 0) or 0)
        multi_day_pnl_usdt = float(reflection_context.get("multi_day_pnl_usdt", 0.0) or 0.0)
        current_equity_usdt = float(reflection_context.get("current_equity_usdt", 0.0) or 0.0)
        configured_initial_usdt = float(reflection_context.get("configured_initial_usdt", 0.0) or 0.0)
        drawdown_pct = float(reflection_context.get("drawdown_pct", 0.0) or 0.0)
        live_trade_expectancy_pct = float(reflection_context.get("live_trade_expectancy_pct", 0.0) or 0.0)
        live_profit_factor = float(reflection_context.get("live_profit_factor", 0.0) or 0.0)
        restore_positive_days = int(reflection_context.get("restore_positive_days", 0) or 0)
        restore_equity_floor_usdt = float(reflection_context.get("restore_equity_floor_usdt", 0.0) or 0.0)
        force_base_only = bool(reflection_context.get("force_fallback_base_only"))
        capital_preservation_mode = bool(reflection_context.get("capital_preservation_mode"))
        preserve_cooldown_scale = reflection_context.get("preserve_cooldown_scale")
        live_symbol_benchmark = reflection_context.get("live_symbol_benchmark") or {}
        current_live_symbol = str(reflection_context.get("current_live_symbol", "") or "").strip()
        previous_controls = reflection_context.get("previous_controls") or {}
        def _tighten_control_max(key: str, target: float) -> None:
            try:
                current = float(controls.get(key, 1.0) or 1.0)
            except (TypeError, ValueError):
                current = 1.0
            controls[key] = min(current, target)

        def _raise_control_min(key: str, target: float) -> None:
            try:
                current = float(controls.get(key, 1.0) or 1.0)
            except (TypeError, ValueError):
                current = 1.0
            controls[key] = max(current, target)

        if top_reject[1] > 0:
            biases.append("prefer execution-valid setups over raw signal frequency until rejection counts normalize")
            risk_adjustments.append(f"treat `{top_reject[0]}` as a first-class constraint in the next 12h window")
        if top_block[1] > 0:
            biases.append(f"reduce candidates that repeatedly hit `{top_block[0]}`")
        if daily_pnl_usdt < 0 and fallback_accepted >= max(3, base_accepted + 2):
            biases.append("fallback-driven entries underperformed in the last window")
            risk_adjustments.append("temporarily require base-strategy alignment before opening new fallback exposure")
            controls["fallback_entry_mode"] = "base_only"
        if accepted_orders > 0 and daily_fees_usdt > max(realized_pnl_usdt, 0.0):
            biases.append("take-profit distance and first profit-lock may be leaving too much edge on the table after fees")
            risk_adjustments.append("review whether first profit-lock clears round-trip fees and whether target distance matches intraday volatility")
        if unrealized_pnl_usdt > max(realized_pnl_usdt, 0.0) * 2 and daily_fees_usdt > 0:
            biases.append("open-profit giveback risk remains high when unrealized gains dominate realized results")
            risk_adjustments.append("prefer earlier profit-lock activation when intraday moves often stall before the current take-profit target")
        if cooldown_blocks >= max(10, total_blocked // 2):
            biases.append("cooldown blocked too many valid opportunities in the last window")
            risk_adjustments.append("shorten cooldown in the next 12h window and re-check if fee bleed stays contained")
            controls["cooldown_scale"] = 0.5
        if carry_in_loss_window_count >= 2:
            biases.append("carry-in positions have repeatedly turned prior-window exposure into PnL drag")
            risk_adjustments.append(
                "tighten hold/stagnation windows for carry-in positions and prefer faster post-exit reassessment until carry-in losses stop repeating"
            )
            controls["carry_in_mode"] = "de_risk"
            _tighten_control_max("hold_bars_scale", 0.5)
            _tighten_control_max("stagnation_bars_scale", 0.5)
            _raise_control_min("stagnation_pnl_scale", 1.35)
        if stagnation_exit_window_count >= 2:
            biases.append("repeated stagnation exits show that too many entries are failing to earn fast follow-through")
            risk_adjustments.append(
                "shorten the allowed hold window and exit nearer flat when continuation fails, rather than letting weak carry-in positions linger"
            )
            _tighten_control_max("hold_bars_scale", 0.75)
            _tighten_control_max("stagnation_bars_scale", 0.75)
            _raise_control_min("stagnation_pnl_scale", 1.15)
        if repeated_benchmark_leader_id and benchmark_leader_streak >= 2:
            biases.append(
                f"`{repeated_benchmark_leader_id}` has led the same live symbol for {benchmark_leader_streak} consecutive windows"
            )
            risk_adjustments.append(
                "treat the repeated benchmark leader as a formal shadow promotion candidate with explicit gate tracking, not just a note in the report"
            )
            controls["benchmark_watch_candidate"] = repeated_benchmark_leader_id
            if current_live_symbol:
                controls["benchmark_watch_symbol"] = current_live_symbol
        if force_base_only:
            biases.append("multi-day drawdown remains active, so fallback entries stay in restricted mode")
            risk_adjustments.append(
                "only restore normal fallback mode after consecutive positive reflection windows and partial equity recovery"
            )
            controls["fallback_entry_mode"] = "base_only"
        if capital_preservation_mode:
            biases.append(
                "multi-day drawdown and negative live expectancy triggered capital-preservation mode"
            )
            risk_adjustments.append(
                "pause new live entries until either equity meaningfully recovers or a benchmark candidate proves positive after costs across consecutive windows"
            )
            controls["entry_mode"] = "capital_preservation"
        previous_carry_in_mode = str(previous_controls.get("carry_in_mode", "") or "").strip().lower()
        if previous_carry_in_mode == "de_risk" and not bool(reflection_context.get("restore_ready")) and negative_streak > 0:
            controls["carry_in_mode"] = "de_risk"
            _tighten_control_max("hold_bars_scale", 0.67)
            _tighten_control_max("stagnation_bars_scale", 0.67)
            _raise_control_min("stagnation_pnl_scale", 1.25)
        if preserve_cooldown_scale is not None:
            try:
                controls["cooldown_scale"] = min(
                    float(controls.get("cooldown_scale", 1.0) or 1.0),
                    float(preserve_cooldown_scale or 1.0),
                )
            except (TypeError, ValueError):
                pass
        benchmark_candidate = {}
        if isinstance(live_symbol_benchmark, dict) and live_symbol_benchmark.get("candidate_id"):
            benchmark_candidate = live_symbol_benchmark
        elif top_benchmark.get("candidate_id"):
            benchmark_candidate = top_benchmark
        if benchmark_candidate.get("candidate_id"):
            biases.append(
                f"keep live strategy honest against external benchmark leader `{benchmark_candidate.get('candidate_id')}`"
            )
            controls["benchmark_watch_candidate"] = str(benchmark_candidate.get("candidate_id", "")).strip()
            controls["benchmark_watch_symbol"] = str(
                benchmark_candidate.get("symbol", current_live_symbol or benchmark_candidate.get("symbol", ""))
            ).strip()
        if not biases:
            biases.append("keep favoring positive expectancy and strong payoff asymmetry")
        if not risk_adjustments:
            risk_adjustments.append("avoid changing thresholds again until the next 12h reflection window")
        summary_parts = [
            f"12h reflection for {slot}: focus on executable positive-expectancy setups",
            f"top blocked={top_block[0]} ({top_block[1]})",
            f"top rejected={top_reject[0]} ({top_reject[1]})",
            f"fallback accepted={fallback_accepted}",
            f"base accepted={base_accepted}",
            f"daily_pnl={daily_pnl_usdt:+.2f}",
        ]
        if lookback_days > 0:
            summary_parts.append(
                f"lookback={lookback_days}d multi_day_pnl={multi_day_pnl_usdt:+.2f} negative_days={negative_day_count} negative_streak={negative_streak}"
            )
        if carry_in_loss_window_count > 0:
            summary_parts.append(
                f"carry_in_loss_windows={carry_in_loss_window_count} carry_in_loss_streak={carry_in_loss_streak}"
            )
        if stagnation_exit_window_count > 0:
            summary_parts.append(
                f"stagnation_exit_windows={stagnation_exit_window_count} stagnation_exit_streak={stagnation_exit_streak}"
            )
        if configured_initial_usdt > 0 and current_equity_usdt > 0:
            summary_parts.append(f"equity={current_equity_usdt:.2f}/{configured_initial_usdt:.2f}")
        if force_base_only:
            summary_parts.append(
                f"fallback locked until positive_streak>={restore_positive_days} and equity>={restore_equity_floor_usdt:.2f}"
            )
        if repeated_benchmark_leader_id and benchmark_leader_streak > 0:
            summary_parts.append(
                f"benchmark_streak={repeated_benchmark_leader_id}x{benchmark_leader_streak}"
            )
        if capital_preservation_mode:
            summary_parts.append(
                f"capital_preservation=on drawdown={drawdown_pct:.2f}% expectancy={live_trade_expectancy_pct:+.2f}% pf={live_profit_factor:.2f}"
            )
        summary = "; ".join(summary_parts) + "."
        return StrategyReflectionSnapshot(
            slot=slot,
            summary=summary,
            biases=biases[:4],
            risk_adjustments=risk_adjustments[:4],
            focus_symbols=focus_symbols,
            controls=self._normalize_controls(controls, previous_controls, reflection_context=reflection_context),
        )

    def _normalize_controls(
        self,
        controls: dict | None,
        fallback: dict | None,
        reflection_context: dict | None = None,
    ) -> dict[str, object]:
        reflection_context = reflection_context or {}
        normalized: dict[str, object] = dict(fallback or {})
        raw = controls or {}
        mode = str(raw.get("fallback_entry_mode", normalized.get("fallback_entry_mode", "normal")) or "normal").strip().lower()
        if mode not in {"normal", "base_only"}:
            mode = "normal"
        normalized["fallback_entry_mode"] = mode
        entry_mode = str(raw.get("entry_mode", normalized.get("entry_mode", "normal")) or "normal").strip().lower()
        if entry_mode not in {"normal", "capital_preservation"}:
            entry_mode = "normal"
        normalized["entry_mode"] = entry_mode
        carry_in_mode = str(raw.get("carry_in_mode", normalized.get("carry_in_mode", "normal")) or "normal").strip().lower()
        if carry_in_mode not in {"normal", "de_risk"}:
            carry_in_mode = "normal"
        normalized["carry_in_mode"] = carry_in_mode
        try:
            cooldown_scale = float(raw.get("cooldown_scale", normalized.get("cooldown_scale", 1.0)) or 1.0)
        except (TypeError, ValueError):
            cooldown_scale = 1.0
        normalized["cooldown_scale"] = max(0.25, min(cooldown_scale, 1.0))
        for key, default, lower, upper in (
            ("hold_bars_scale", 1.0, 0.5, 1.0),
            ("stagnation_bars_scale", 1.0, 0.5, 1.0),
            ("stagnation_pnl_scale", 1.0, 0.75, 1.5),
        ):
            try:
                value = float(raw.get(key, normalized.get(key, default)) or default)
            except (TypeError, ValueError):
                value = default
            normalized[key] = max(lower, min(value, upper))
        for key in ("benchmark_watch_candidate", "benchmark_watch_symbol"):
            value = str(raw.get(key, normalized.get(key, "")) or "").strip()
            if value:
                normalized[key] = value
            else:
                normalized.pop(key, None)
        if bool(reflection_context.get("force_fallback_base_only")):
            normalized["fallback_entry_mode"] = "base_only"
        if bool(reflection_context.get("capital_preservation_mode")):
            normalized["entry_mode"] = "capital_preservation"
        if str((reflection_context.get("previous_controls") or {}).get("carry_in_mode", "") or "").strip().lower() == "de_risk":
            if not bool(reflection_context.get("restore_ready")) and int(reflection_context.get("negative_streak", 0) or 0) > 0:
                normalized["carry_in_mode"] = "de_risk"
                normalized["hold_bars_scale"] = min(float(normalized.get("hold_bars_scale", 1.0) or 1.0), 0.67)
                normalized["stagnation_bars_scale"] = min(float(normalized.get("stagnation_bars_scale", 1.0) or 1.0), 0.67)
                normalized["stagnation_pnl_scale"] = max(float(normalized.get("stagnation_pnl_scale", 1.0) or 1.0), 1.25)
        preserve_cooldown_scale = reflection_context.get("preserve_cooldown_scale")
        if preserve_cooldown_scale is not None:
            try:
                normalized["cooldown_scale"] = min(
                    float(normalized.get("cooldown_scale", 1.0) or 1.0),
                    max(0.25, min(float(preserve_cooldown_scale or 1.0), 1.0)),
                )
            except (TypeError, ValueError):
                pass
        live_symbol_benchmark = reflection_context.get("live_symbol_benchmark") or {}
        current_live_symbol = str(reflection_context.get("current_live_symbol", "") or "").strip()
        live_benchmark_positive_edge = (
            isinstance(live_symbol_benchmark, dict)
            and float(live_symbol_benchmark.get("expectancy_pct", 0.0) or 0.0) > 0.0
            and float(live_symbol_benchmark.get("profit_factor", 0.0) or 0.0) > 1.0
        )
        if live_benchmark_positive_edge and live_symbol_benchmark.get("candidate_id"):
            normalized["benchmark_watch_candidate"] = str(live_symbol_benchmark.get("candidate_id", "")).strip()
            normalized["benchmark_watch_symbol"] = str(live_symbol_benchmark.get("symbol", current_live_symbol)).strip()
        elif current_live_symbol:
            normalized["benchmark_watch_symbol"] = current_live_symbol
        return normalized


class SelectorAgent:
    name = "selector"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def select(self, candidates: list[dict], strategy_memory: dict | None = None) -> tuple[dict, str]:
        def expectancy(item: dict) -> float:
            selected = item.get("selected_strategy_backtest") or item.get("backtest", {})
            return float(selected.get("expectancy_pct", item["backtest"].get("expectancy_pct", 0.0)))

        def rank(item: dict) -> tuple:
            approval = item["approval"]
            reason = approval["reason"]
            executable_hint = 0
            if reason in {"no base asset available to sell", "not enough USDT to open a position"}:
                executable_hint = -1
            return (
                approval["approved"],
                executable_hint,
                item["idea"]["action"] != "hold",
                expectancy(item),
                float(item["idea"]["score"]),
                float(item["backtest"]["cumulative_return_pct"]),
            )

        ranked = sorted(
            candidates,
            key=rank,
            reverse=True,
        )
        llm_choice = self._llm_select(candidates, ranked[0], strategy_memory)
        if llm_choice is not None:
            return llm_choice
        chosen = ranked[0]
        if chosen["approval"]["approved"]:
            summary = (
                f"selected {chosen['symbol']} with {chosen['idea']['action']} "
                f"score={float(chosen['idea']['score']):.2f}"
            )
        elif chosen["idea"]["action"] == "hold":
            summary = (
                f"no executable candidate; best observe-only symbol was {chosen['symbol']} "
                f"(hold, score={float(chosen['idea']['score']):.2f})"
            )
        else:
            summary = (
                f"no symbol approved; closest candidate was {chosen['symbol']} "
                f"({chosen['idea']['action']}, {chosen['approval']['reason']})"
            )
        return chosen, summary

    def _llm_select(
        self,
        candidates: list[dict],
        fallback: dict,
        strategy_memory: dict | None = None,
    ) -> tuple[dict, str] | None:
        if self.llm_client is None:
            return None
        candidate_payload = []
        for item in candidates:
            selected = item.get("selected_strategy_backtest") or item.get("backtest", {})
            candidate_payload.append(
                {
                    "symbol": item["symbol"],
                    "action": item["idea"]["action"],
                    "score": round(float(item["idea"]["score"]), 4),
                    "approval": bool(item["approval"]["approved"]),
                    "approval_reason": item["approval"]["reason"],
                    "selection_edge_expectancy_pct": round(float(selected.get("expectancy_pct", 0.0)), 4),
                    "selection_profit_factor": round(float(selected.get("profit_factor", 0.0)), 4),
                    "baseline_cumulative_return_pct": round(float(item["backtest"]["cumulative_return_pct"]), 4),
                    "strategy_summary": item["strategy_research"]["summary"],
                    "account": item.get("account", {}),
                    "strategy_memory_focus": (strategy_memory or {}).get("focus_symbols", []),
                }
            )
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the selector agent in a crypto trading system. "
                    "Choose the best symbol candidate for this cycle. "
                    "Return JSON with keys symbol and summary. "
                    "Prefer executable ideas. If no candidate is executable, pick the symbol with the best "
                    "positive-expectancy watch setup instead of blindly defaulting to hold. "
                    f"strategy_memory={json.dumps(strategy_memory or {}, ensure_ascii=False)}; "
                    f"fallback_symbol={fallback['symbol']}; "
                    f"candidates={json.dumps(candidate_payload, ensure_ascii=False)}"
                )
            )
        except Exception:
            return None
        symbol = str(response.get("symbol", fallback["symbol"]))
        summary = str(response.get("summary", "")).strip()
        chosen = next((item for item in candidates if item["symbol"] == symbol), None)
        if chosen is None:
            return None
        if not summary:
            return None
        return chosen, summary
