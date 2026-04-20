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
        if current_signal == "short" and can_sell and sell_sentiment_ok and (
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
        if current_signal == "long" and can_buy and buy_sentiment_ok and (
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
            if opening_long or opening_short:
                projected_exposure += max_notional
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
                    "Return JSON with keys title, operations_summary, decision_summary, improvement_directions. "
                    "improvement_directions must be a short array of concrete next steps. "
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
            return DailyReviewSnapshot(
                title=str(response.get("title", fallback.title)).strip() or fallback.title,
                operations_summary=str(response.get("operations_summary", fallback.operations_summary)).strip()
                or fallback.operations_summary,
                decision_summary=str(response.get("decision_summary", fallback.decision_summary)).strip()
                or fallback.decision_summary,
                improvement_directions=normalized_directions[:5],
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
        action_line = ", ".join(f"{key}={value}" for key, value in action_counts.items()) or "no actions"
        symbol_line = ", ".join(f"{key}={value}" for key, value in selected_symbol_counts.items()) or "no symbol focus"
        top_block = next(iter(blocked_reason_counts.items()), ("none", 0))
        top_reject = next(iter(rejection_reason_counts.items()), ("none", 0))
        top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
        top_alpha = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]

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
        if top_benchmark.get("candidate_id"):
            decision_summary += (
                f" 最新外部 benchmark 目前以 {top_benchmark.get('candidate_id', 'n/a')} "
                f"@ {top_benchmark.get('symbol', 'n/a')} 領先，"
                f"expectancy {float(top_benchmark.get('expectancy_pct', 0.0)):+.2f}%。"
            )
        if top_alpha.get("candidate_id"):
            decision_summary += (
                f" Alpha Arena 對照組領先的是 {top_alpha.get('candidate_id', 'n/a')}。"
            )

        improvements: list[str] = []
        if float(financial.get("daily_fees_usdt", 0.0)) > max(float(financial.get("daily_pnl_usdt", 0.0)), 0.0):
            improvements.append("手續費已接近或超過當日獲利，優先降低過度交易與低品質微型訊號。")
        if daily_summary.get("rejected_orders", 0) > 0:
            improvements.append("在 executor 前補一層交易所最小單與最終下單 notional 檢查，避免把 rejected 當成有效成交。")
        if top_block[0] != "none" and top_block[1] > 0:
            improvements.append(f"優先處理 `{top_block[0]}`，降低可執行候選被風控或交易所門檻擋下的比例。")
        if len(selected_symbol_counts) <= 2 and sum(selected_symbol_counts.values()) > 20:
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

        return DailyReviewSnapshot(
            title=f"Trading Agents Daily Review - {date_label}",
            operations_summary=operations_summary,
            decision_summary=decision_summary,
            improvement_directions=improvements[:4],
        )


class StrategyReflectionAgent:
    name = "strategy_reflector"

    def __init__(self, llm_client: OllamaClient | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(self, slot: str, daily_summary: dict) -> StrategyReflectionSnapshot:
        fallback = self._fallback(slot, daily_summary)
        if self.llm_client is None:
            return fallback
        try:
            response = self.llm_client.generate_json(
                (
                    "You are the strategy reflection agent for a crypto trading system. "
                    "This reflection runs only once every 12 hours to avoid overfitting. "
                    "Return JSON with keys summary, biases, risk_adjustments, focus_symbols. "
                    f"slot={slot}; daily_summary={json.dumps(daily_summary, ensure_ascii=False)}"
                )
            )
            biases = response.get("biases", fallback.biases)
            adjustments = response.get("risk_adjustments", fallback.risk_adjustments)
            focus_symbols = response.get("focus_symbols", fallback.focus_symbols)
            if not isinstance(biases, list):
                biases = fallback.biases
            if not isinstance(adjustments, list):
                adjustments = fallback.risk_adjustments
            if not isinstance(focus_symbols, list):
                focus_symbols = fallback.focus_symbols
            return StrategyReflectionSnapshot(
                slot=slot,
                summary=str(response.get("summary", fallback.summary)).strip() or fallback.summary,
                biases=[str(item).strip() for item in biases if str(item).strip()][:4] or fallback.biases,
                risk_adjustments=[str(item).strip() for item in adjustments if str(item).strip()][:4] or fallback.risk_adjustments,
                focus_symbols=[str(item).strip() for item in focus_symbols if str(item).strip()][:4] or fallback.focus_symbols,
            )
        except Exception:
            return fallback

    def _fallback(self, slot: str, daily_summary: dict) -> StrategyReflectionSnapshot:
        blocked = daily_summary.get("blocked_reason_counts", {})
        rejected = daily_summary.get("rejection_reason_counts", {})
        selected = daily_summary.get("selected_symbol_counts", {})
        top_block = next(iter(blocked.items()), ("none", 0))
        top_reject = next(iter(rejected.items()), ("none", 0))
        external_benchmarks = daily_summary.get("external_benchmarks", {})
        top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
        focus_symbols = [key for key, _ in list(selected.items())[:3]]
        biases: list[str] = []
        risk_adjustments: list[str] = []
        if top_reject[1] > 0:
            biases.append("prefer execution-valid setups over raw signal frequency until rejection counts normalize")
            risk_adjustments.append(f"treat `{top_reject[0]}` as a first-class constraint in the next 12h window")
        if top_block[1] > 0:
            biases.append(f"reduce candidates that repeatedly hit `{top_block[0]}`")
        if top_benchmark.get("candidate_id"):
            biases.append(
                f"keep live strategy honest against external benchmark leader `{top_benchmark.get('candidate_id')}`"
            )
        if not biases:
            biases.append("keep favoring positive expectancy and strong payoff asymmetry")
        if not risk_adjustments:
            risk_adjustments.append("avoid changing thresholds again until the next 12h reflection window")
        summary = (
            f"12h reflection for {slot}: focus on executable positive-expectancy setups; "
            f"top blocked={top_block[0]} ({top_block[1]}); top rejected={top_reject[0]} ({top_reject[1]}); "
            f"external benchmark leader={top_benchmark.get('candidate_id', 'n/a')}."
        )
        return StrategyReflectionSnapshot(
            slot=slot,
            summary=summary,
            biases=biases[:4],
            risk_adjustments=risk_adjustments[:4],
            focus_symbols=focus_symbols,
        )


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
