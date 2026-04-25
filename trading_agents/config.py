from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv() -> bool:
        return False


load_dotenv()


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    model_backend: str = os.getenv("MODEL_BACKEND", "ollama")
    model_name: str = os.getenv("MODEL_NAME", "qwen2.5:7b-instruct")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    trading_mode: str = os.getenv("TRADING_MODE", "mock")
    symbol: str = os.getenv("SYMBOL", "BTC/USDT")
    observation_pool: tuple[str, ...] = _list(
        "OBSERVATION_POOL",
        "SOL/USDT,LINK/USDT,AVAX/USDT",
    )
    timeframe: str = os.getenv("TIMEFRAME", "15m")
    data_root: str = os.getenv("DATA_ROOT", "./runtime")
    sentiment_config_path: str = os.getenv("SENTIMENT_CONFIG_PATH", "./config/sentiment_sources.json")
    strategy_library_path: str = os.getenv("STRATEGY_LIBRARY_PATH", "./config/strategy_library.json")
    external_benchmark_library_path: str = os.getenv(
        "EXTERNAL_BENCHMARK_LIBRARY_PATH",
        "./config/external_benchmark_library.json",
    )
    monitor_interval_seconds: float = _float("MONITOR_INTERVAL_SECONDS", 30.0)
    run_interval_seconds: float = _float("RUN_INTERVAL_SECONDS", 900.0)
    price_trigger_pct: float = _float("PRICE_TRIGGER_PCT", 0.0075)
    initial_balance_usdt: float = _float("INITIAL_BALANCE_USDT", 500.0)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.40)
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.03)
    min_signal_score: float = _float("MIN_SIGNAL_SCORE", 0.55)
    taker_fee_pct: float = _float("TAKER_FEE_PCT", 0.001)
    binance_testnet_api_key: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
    binance_testnet_secret: str = os.getenv("BINANCE_TESTNET_SECRET", "")
    bybit_demo_api_key: str = os.getenv("BYBIT_DEMO_API_KEY", "")
    bybit_demo_secret: str = os.getenv("BYBIT_DEMO_SECRET", "")
    notion_api_token: str = os.getenv("NOTION_API_TOKEN", "")
    notion_status_page_id: str = os.getenv("NOTION_STATUS_PAGE_ID", "")
    notion_status_page_title: str = os.getenv("NOTION_STATUS_PAGE_TITLE", "Trading Agents Live Status")
    notion_heartbeat_sync_seconds: float = _float("NOTION_HEARTBEAT_SYNC_SECONDS", 300.0)
    notion_daily_review_parent_page_id: str = os.getenv("NOTION_DAILY_REVIEW_PARENT_PAGE_ID", "")
    notion_daily_review_title_prefix: str = os.getenv("NOTION_DAILY_REVIEW_TITLE_PREFIX", "Trading Agents Daily Review")
    notion_daily_review_hour: float = _float("NOTION_DAILY_REVIEW_HOUR", 12.0)
    external_ai_review_enabled: bool = _bool("EXTERNAL_AI_REVIEW_ENABLED", False)
    external_ai_review_provider: str = os.getenv("EXTERNAL_AI_REVIEW_PROVIDER", "gemini")
    external_ai_review_model: str = os.getenv("EXTERNAL_AI_REVIEW_MODEL", "gemini-2.5-flash")
    external_ai_review_api_key: str = os.getenv("EXTERNAL_AI_REVIEW_API_KEY", os.getenv("GEMINI_API_KEY", ""))
    external_ai_review_timeout_seconds: float = _float("EXTERNAL_AI_REVIEW_TIMEOUT_SECONDS", 20.0)
    external_benchmark_enabled: bool = _bool("EXTERNAL_BENCHMARK_ENABLED", True)
    external_benchmark_refresh_hours: float = _float("EXTERNAL_BENCHMARK_REFRESH_HOURS", 4.0)
    external_benchmark_limit: int = _int("EXTERNAL_BENCHMARK_LIMIT", 320)
    external_benchmark_max_alpha_signals: int = _int("EXTERNAL_BENCHMARK_MAX_ALPHA_SIGNALS", 1000)
    llm_timeout_seconds: float = _float("LLM_TIMEOUT_SECONDS", 18.0)
    sentiment_request_timeout_seconds: float = _float("SENTIMENT_REQUEST_TIMEOUT_SECONDS", 6.0)
    sentiment_cache_ttl_seconds: float = _float("SENTIMENT_CACHE_TTL_SECONDS", 120.0)
    llm_full_cycle_only: bool = _bool("LLM_FULL_CYCLE_ONLY", True)
    llm_selected_candidate_only: bool = _bool("LLM_SELECTED_CANDIDATE_ONLY", True)
    llm_wake_gate_enabled: bool = _bool("LLM_WAKE_GATE_ENABLED", True)
    llm_wake_min_score: float = _float("LLM_WAKE_MIN_SCORE", 3.0)
    llm_wake_position_min_score: float = _float("LLM_WAKE_POSITION_MIN_SCORE", 1.0)
    llm_wake_volatility_pct: float = _float("LLM_WAKE_VOLATILITY_PCT", 0.25)
    llm_wake_momentum_pct: float = _float("LLM_WAKE_MOMENTUM_PCT", 0.20)
    llm_wake_volume_ratio: float = _float("LLM_WAKE_VOLUME_RATIO", 1.25)
    llm_wake_breakout_proximity_pct: float = _float("LLM_WAKE_BREAKOUT_PROXIMITY_PCT", 0.12)
    llm_wake_position_move_pct: float = _float("LLM_WAKE_POSITION_MOVE_PCT", 0.20)
    llm_wake_depth_imbalance: float = _float("LLM_WAKE_DEPTH_IMBALANCE", 0.22)
    llm_wake_trade_delta_ratio: float = _float("LLM_WAKE_TRADE_DELTA_RATIO", 0.25)
    llm_wake_large_trade_count: int = _int("LLM_WAKE_LARGE_TRADE_COUNT", 3)
    llm_wake_quiet_volatility_pct: float = _float("LLM_WAKE_QUIET_VOLATILITY_PCT", 0.12)
    llm_wake_quiet_volume_ratio: float = _float("LLM_WAKE_QUIET_VOLUME_RATIO", 0.95)
    market_microstructure_enabled: bool = _bool("MARKET_MICROSTRUCTURE_ENABLED", True)
    orderbook_depth_limit: int = _int("ORDERBOOK_DEPTH_LIMIT", 25)
    recent_public_trade_limit: int = _int("RECENT_PUBLIC_TRADE_LIMIT", 60)
    microstructure_cache_ttl_seconds: float = _float("MICROSTRUCTURE_CACHE_TTL_SECONDS", 5.0)
    demo_aggressive_mode: bool = _bool("DEMO_AGGRESSIVE_MODE", True)
    expectancy_floor_pct: float = _float("EXPECTANCY_FLOOR_PCT", -0.03)
    micro_cycle_trigger_pct: float = _float("MICRO_CYCLE_TRIGGER_PCT", 0.0025)
    position_micro_trigger_pct: float = _float("POSITION_MICRO_TRIGGER_PCT", 0.0020)
    trade_cooldown_seconds: float = _float("TRADE_COOLDOWN_SECONDS", 900.0)
    trade_cooldown_single_symbol_cap_seconds: float = _float("TRADE_COOLDOWN_SINGLE_SYMBOL_CAP_SECONDS", 300.0)
    trade_cooldown_trend_multiplier: float = _float("TRADE_COOLDOWN_TREND_MULTIPLIER", 0.35)
    trade_cooldown_min_seconds: float = _float("TRADE_COOLDOWN_MIN_SECONDS", 120.0)
    trade_cooldown_reentry_momentum_pct: float = _float("TRADE_COOLDOWN_REENTRY_MOMENTUM_PCT", 0.25)
    trade_cooldown_reentry_trade_delta_ratio: float = _float("TRADE_COOLDOWN_REENTRY_TRADE_DELTA_RATIO", 0.35)
    trade_cooldown_reentry_volume_ratio: float = _float("TRADE_COOLDOWN_REENTRY_VOLUME_RATIO", 1.20)
    fallback_range_guard_enabled: bool = _bool("FALLBACK_RANGE_GUARD_ENABLED", True)
    fallback_range_guard_adx_max: float = _float("FALLBACK_RANGE_GUARD_ADX_MAX", 18.0)
    fallback_range_guard_volume_ratio: float = _float("FALLBACK_RANGE_GUARD_VOLUME_RATIO", 1.35)
    fallback_range_guard_trade_delta_ratio: float = _float("FALLBACK_RANGE_GUARD_TRADE_DELTA_RATIO", 0.45)
    fallback_entry_guard_enabled: bool = _bool("FALLBACK_ENTRY_GUARD_ENABLED", True)
    fallback_entry_min_score: float = _float("FALLBACK_ENTRY_MIN_SCORE", 0.72)
    fallback_entry_min_momentum_pct: float = _float("FALLBACK_ENTRY_MIN_MOMENTUM_PCT", 0.20)
    fallback_entry_min_volume_ratio: float = _float("FALLBACK_ENTRY_MIN_VOLUME_RATIO", 1.25)
    fallback_entry_min_trade_delta_ratio: float = _float("FALLBACK_ENTRY_MIN_TRADE_DELTA_RATIO", 0.30)
    buy_balance_buffer_pct: float = _float("BUY_BALANCE_BUFFER_PCT", 0.95)
    fee_hurdle_multiplier: float = _float("FEE_HURDLE_MULTIPLIER", 1.15)
    fast_cycle_signal_boost: float = _float("FAST_CYCLE_SIGNAL_BOOST", 0.08)
    dust_position_multiplier: float = _float("DUST_POSITION_MULTIPLIER", 1.0)
    perp_max_leverage: float = _float("PERP_MAX_LEVERAGE", 2.0)
    perp_min_available_balance_ratio_pct: float = _float("PERP_MIN_AVAILABLE_BALANCE_RATIO_PCT", 10.0)
    perp_min_liquidation_buffer_pct: float = _float("PERP_MIN_LIQUIDATION_BUFFER_PCT", 8.0)
    perp_hard_stop_loss_pct: float = _float("PERP_HARD_STOP_LOSS_PCT", 1.2)
    perp_take_profit_pct: float = _float("PERP_TAKE_PROFIT_PCT", 2.4)
    perp_trailing_stop_pct: float = _float("PERP_TRAILING_STOP_PCT", 0.0)
    perp_enable_protection_orders: bool = _bool("PERP_ENABLE_PROTECTION_ORDERS", True)
    perp_profit_lock_trigger_pct: float = _float("PERP_PROFIT_LOCK_TRIGGER_PCT", 1.0)
    perp_profit_lock_breakeven_offset_pct: float = _float("PERP_PROFIT_LOCK_BREAKEVEN_OFFSET_PCT", 0.10)
    perp_profit_lock_trigger_2_pct: float = _float("PERP_PROFIT_LOCK_TRIGGER_2_PCT", 2.0)
    perp_profit_lock_stop_2_pct: float = _float("PERP_PROFIT_LOCK_STOP_2_PCT", 0.80)
    intraday_max_hold_bars: float = _float("INTRADAY_MAX_HOLD_BARS", 12.0)
    intraday_stagnation_bars: float = _float("INTRADAY_STAGNATION_BARS", 4.0)
    intraday_stagnation_pnl_pct: float = _float("INTRADAY_STAGNATION_PNL_PCT", 0.35)
    intraday_force_flat_enabled: bool = _bool("INTRADAY_FORCE_FLAT_ENABLED", False)
    intraday_force_flat_hour_local: float = _float("INTRADAY_FORCE_FLAT_HOUR_LOCAL", 23.0)
    intraday_force_flat_minute_local: float = _float("INTRADAY_FORCE_FLAT_MINUTE_LOCAL", 45.0)
    strategy_learning_lookback_days: int = _int("STRATEGY_LEARNING_LOOKBACK_DAYS", 5)
    strategy_learning_negative_day_threshold: int = _int("STRATEGY_LEARNING_NEGATIVE_DAY_THRESHOLD", 2)
    strategy_learning_restore_positive_days: int = _int("STRATEGY_LEARNING_RESTORE_POSITIVE_DAYS", 2)
    strategy_learning_restore_equity_recovery_ratio_pct: float = _float(
        "STRATEGY_LEARNING_RESTORE_EQUITY_RECOVERY_RATIO_PCT",
        99.0,
    )


def load_settings() -> Settings:
    return Settings()
