from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timeframe: str
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    last_price: float
    best_bid_price: float = 0.0
    best_ask_price: float = 0.0
    spread_bps: float = 0.0
    top_bid_size: float = 0.0
    top_ask_size: float = 0.0
    top_book_imbalance: float = 0.0
    depth_bid_notional: float = 0.0
    depth_ask_notional: float = 0.0
    depth_imbalance: float = 0.0
    bid_wall_price: float = 0.0
    ask_wall_price: float = 0.0
    bid_wall_notional: float = 0.0
    ask_wall_notional: float = 0.0
    bid_wall_distance_bps: float = 0.0
    ask_wall_distance_bps: float = 0.0
    trade_buy_notional: float = 0.0
    trade_sell_notional: float = 0.0
    trade_delta_notional: float = 0.0
    trade_delta_ratio: float = 0.0
    aggressive_buy_ratio: float = 0.0
    aggressive_sell_ratio: float = 0.0
    recent_trade_count: int = 0
    large_buy_count: int = 0
    large_sell_count: int = 0
    orderbook_levels: int = 0


@dataclass(frozen=True)
class SentimentSnapshot:
    source_count: int
    sentiment_score: float
    summary: str
    references: list[str]


@dataclass(frozen=True)
class TradeIdea:
    action: str
    score: float
    rationale: str
    invalidation: str
    holding_horizon: str


@dataclass(frozen=True)
class BacktestSnapshot:
    sample_count: int
    trade_count: int
    win_rate: float
    avg_return_pct: float
    cumulative_return_pct: float
    summary: str
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy_pct: float = 0.0
    profit_factor: float = 0.0


@dataclass(frozen=True)
class StrategyCandidate:
    strategy_id: str
    name: str
    source: str
    credibility: str
    description: str
    backtest: BacktestSnapshot


@dataclass(frozen=True)
class StrategyResearchSnapshot:
    base_strategy_id: str
    selected_strategy_id: str
    selected_strategy_name: str
    summary: str
    candidates: list[StrategyCandidate]
    selected_strategy_rationale: str = ""


@dataclass(frozen=True)
class Approval:
    approved: bool
    reason: str
    max_notional_usdt: float
    warnings: list[str]


@dataclass(frozen=True)
class EvaluationReport:
    grade: str
    notes: str


@dataclass(frozen=True)
class DailyReviewSnapshot:
    title: str
    operations_summary: str
    decision_summary: str
    improvement_directions: list[str]


@dataclass(frozen=True)
class StrategyReflectionSnapshot:
    slot: str
    summary: str
    biases: list[str]
    risk_adjustments: list[str]
    focus_symbols: list[str]
