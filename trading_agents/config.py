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
    monitor_interval_seconds: float = _float("MONITOR_INTERVAL_SECONDS", 30.0)
    run_interval_seconds: float = _float("RUN_INTERVAL_SECONDS", 900.0)
    price_trigger_pct: float = _float("PRICE_TRIGGER_PCT", 0.0075)
    initial_balance_usdt: float = _float("INITIAL_BALANCE_USDT", 150.0)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.20)
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
    llm_timeout_seconds: float = _float("LLM_TIMEOUT_SECONDS", 18.0)
    sentiment_request_timeout_seconds: float = _float("SENTIMENT_REQUEST_TIMEOUT_SECONDS", 6.0)
    sentiment_cache_ttl_seconds: float = _float("SENTIMENT_CACHE_TTL_SECONDS", 120.0)
    llm_full_cycle_only: bool = _bool("LLM_FULL_CYCLE_ONLY", True)
    llm_selected_candidate_only: bool = _bool("LLM_SELECTED_CANDIDATE_ONLY", True)
    llm_wake_gate_enabled: bool = _bool("LLM_WAKE_GATE_ENABLED", True)
    llm_wake_min_score: float = _float("LLM_WAKE_MIN_SCORE", 2.0)
    llm_wake_position_min_score: float = _float("LLM_WAKE_POSITION_MIN_SCORE", 1.0)
    llm_wake_volatility_pct: float = _float("LLM_WAKE_VOLATILITY_PCT", 0.15)
    llm_wake_momentum_pct: float = _float("LLM_WAKE_MOMENTUM_PCT", 0.12)
    llm_wake_volume_ratio: float = _float("LLM_WAKE_VOLUME_RATIO", 1.15)
    llm_wake_breakout_proximity_pct: float = _float("LLM_WAKE_BREAKOUT_PROXIMITY_PCT", 0.20)
    llm_wake_position_move_pct: float = _float("LLM_WAKE_POSITION_MOVE_PCT", 0.20)
    demo_aggressive_mode: bool = _bool("DEMO_AGGRESSIVE_MODE", True)
    expectancy_floor_pct: float = _float("EXPECTANCY_FLOOR_PCT", -0.03)
    micro_cycle_trigger_pct: float = _float("MICRO_CYCLE_TRIGGER_PCT", 0.0025)
    position_micro_trigger_pct: float = _float("POSITION_MICRO_TRIGGER_PCT", 0.0020)
    trade_cooldown_seconds: float = _float("TRADE_COOLDOWN_SECONDS", 900.0)
    buy_balance_buffer_pct: float = _float("BUY_BALANCE_BUFFER_PCT", 0.95)
    fee_hurdle_multiplier: float = _float("FEE_HURDLE_MULTIPLIER", 1.15)
    fast_cycle_signal_boost: float = _float("FAST_CYCLE_SIGNAL_BOOST", 0.08)
    dust_position_multiplier: float = _float("DUST_POSITION_MULTIPLIER", 1.0)


def load_settings() -> Settings:
    return Settings()
