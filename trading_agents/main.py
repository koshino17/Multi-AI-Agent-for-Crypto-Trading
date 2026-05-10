from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import fmean
from time import perf_counter, sleep
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

from trading_agents.agents import (
    DailyReviewAgent,
    ExecutorAgent,
    MarketCollectorAgent,
    OrderFlowCollectorAgent,
    PostTradeEvaluatorAgent,
    RiskSupervisorAgent,
    SelectorAgent,
    SentimentCollectorAgent,
    StrategistAgent,
    StrategyReflectionAgent,
)
from trading_agents.backtest import BacktestAgent
from trading_agents.config import load_settings
from trading_agents.external_benchmarks import (
    load_external_benchmark_summary,
    refresh_external_benchmark_suite,
)
from trading_agents.external_ai_review import (
    external_ai_review_path,
    generate_external_ai_review,
    load_external_ai_review,
    save_external_ai_review,
)
from trading_agents.exchange import (
    BinanceTestnetExchangeClient,
    BybitDemoExchangeClient,
    BybitDemoPerpExchangeClient,
    MockExchangeClient,
)
from trading_agents.llm import OllamaClient
from trading_agents.models import (
    BacktestSnapshot,
    SentimentSnapshot,
    StrategyCandidate,
    StrategyResearchSnapshot,
    TradeIdea,
)
from trading_agents.notion_sync import sync_notion_daily_review, sync_notion_status
from trading_agents.reporting import (
    LOCAL_TZ,
    REPORT_WINDOW_ANCHOR_HOUR_LOCAL,
    build_human_report,
    build_daily_summary,
    completed_report_date_label,
    load_daily_summary_data,
    local_date_label,
    update_equity_curve,
    write_human_report,
    write_json_log,
    write_daily_summary,
)
from trading_agents.research import StrategyResearchAgent
from trading_agents.sentiment import SentimentDataProvider, write_sentiment_record
from trading_agents.storage import build_storage_layout, mode_scoped_path
from trading_agents.strategy_memory import current_strategy_slot, load_strategy_memory, save_strategy_memory


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _daily_strategy_review_path(storage, date_label: str) -> Path:
    return storage.service / f"daily_strategy_review-{date_label}.json"


def _write_daily_strategy_review(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _daily_review_fingerprint(daily_summary: dict) -> str:
    financial = daily_summary.get("financial_snapshot") or {}
    loss = daily_summary.get("loss_attribution") or {}
    postmortem = daily_summary.get("symbol_postmortem") or {}
    external_benchmarks = daily_summary.get("external_benchmarks") or {}
    top_by_symbol = external_benchmarks.get("top_by_symbol") or {}
    if not isinstance(top_by_symbol, dict):
        top_by_symbol = {}
    focus_symbol = str(postmortem.get("symbol", "") or "").strip()
    focus_benchmark = top_by_symbol.get(focus_symbol, {}) if focus_symbol else {}
    if not isinstance(focus_benchmark, dict):
        focus_benchmark = {}
    strategy_memory_current = daily_summary.get("strategy_memory_current") or {}
    current_controls = strategy_memory_current.get("controls") if isinstance(strategy_memory_current, dict) else {}
    if not isinstance(current_controls, dict):
        current_controls = {}
    payload = {
        "mode": daily_summary.get("mode"),
        "date_label": daily_summary.get("date_label", ""),
        "daily_pnl_usdt": round(float(financial.get("daily_pnl_usdt", 0.0) or 0.0), 4),
        "realized_pnl_usdt": round(float(financial.get("realized_pnl_usdt", 0.0) or 0.0), 4),
        "daily_fees_usdt": round(float(financial.get("daily_fees_usdt", 0.0) or 0.0), 4),
        "carry_in_closed_count": int(loss.get("carry_in_closed_count", 0) or 0),
        "new_closed_count": int(loss.get("new_closed_count", 0) or 0),
        "primary_driver": str(loss.get("primary_driver", "") or ""),
        "focus_symbol": focus_symbol,
        "focus_benchmark_candidate": str(focus_benchmark.get("candidate_id", "") or ""),
        "focus_benchmark_expectancy": round(float(focus_benchmark.get("expectancy_pct", 0.0) or 0.0), 4),
        "benchmark_watch_candidate": str(current_controls.get("benchmark_watch_candidate", "") or ""),
        "benchmark_watch_symbol": str(current_controls.get("benchmark_watch_symbol", "") or ""),
        "entry_mode": str(current_controls.get("entry_mode", "") or ""),
        "carry_in_mode": str(current_controls.get("carry_in_mode", "") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _load_recent_daily_review_history(storage, current_date_label: str, lookback_days: int = 4) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for label in _recent_local_date_labels(max(lookback_days, 1)):
        if label == current_date_label:
            continue
        payload = _read_json_file(_daily_strategy_review_path(storage, label))
        if not isinstance(payload, dict) or not payload:
            continue
        history.append(
            {
                "date_label": label,
                "improvement_directions": payload.get("improvement_directions", []),
                "action_items": payload.get("action_items", []),
                "consensus_summary": payload.get("consensus_summary", ""),
            }
        )
    return history


def _load_trade_cooldowns(path: Path) -> dict[str, float]:
    raw = _read_json_file(path)
    cooldowns: dict[str, float] = {}
    for symbol, until in raw.items():
        try:
            cooldowns[str(symbol)] = float(until)
        except (TypeError, ValueError):
            continue
    return cooldowns


def _save_trade_cooldowns(path: Path, cooldowns: dict[str, float]) -> None:
    path.write_text(json.dumps(cooldowns, ensure_ascii=False, indent=2))


def _cooldown_key(mode: str, symbol: str) -> str:
    return f"{mode}:{symbol}"


def _cooldown_remaining_seconds(cooldowns: dict[str, float], mode: str, symbol: str, now_epoch: float) -> float:
    exact_key = _cooldown_key(mode, symbol)
    if exact_key in cooldowns:
        return max(0.0, float(cooldowns.get(exact_key, 0.0)) - now_epoch)
    if "perp" in mode:
        return 0.0
    return max(0.0, float(cooldowns.get(symbol, 0.0)) - now_epoch)


def _recent_local_date_labels(count: int, *, end: datetime | None = None) -> list[str]:
    if count <= 0:
        return []
    local_end = end.astimezone(LOCAL_TZ) if end is not None else datetime.now(LOCAL_TZ)
    return [
        (local_end - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(count - 1, -1, -1)
    ]


def _build_strategy_reflection_context(settings, storage, current_date_label: str, daily_summary: dict, previous_memory: dict) -> dict[str, object]:
    live_symbols = _parse_symbol_pool(None, settings)
    current_live_symbol = live_symbols[0] if len(live_symbols) == 1 else ""
    lookback_days = max(int(settings.strategy_learning_lookback_days or 0), 1)
    recent_windows: list[dict[str, object]] = []
    for label in _recent_local_date_labels(lookback_days):
        try:
            summary = load_daily_summary_data(storage.trade_logs, label, storage.runner_log)
        except Exception:
            continue
        if not isinstance(summary, dict):
            continue
        financial = summary.get("financial_snapshot") or {}
        loss_attribution = summary.get("loss_attribution") or {}
        policy_exit_diagnostics = summary.get("policy_exit_diagnostics") or {}
        live_symbol_benchmark = summary.get("benchmark_watch_candidate_current") or {}
        if not isinstance(live_symbol_benchmark, dict) or not str(live_symbol_benchmark.get("candidate_id", "") or "").strip():
            external_benchmarks = summary.get("external_benchmarks") or {}
            top_by_symbol = external_benchmarks.get("top_by_symbol") or {}
            if not isinstance(top_by_symbol, dict):
                top_by_symbol = {}
            live_symbol_benchmark = top_by_symbol.get(current_live_symbol, {}) if current_live_symbol else {}
        if not isinstance(live_symbol_benchmark, dict):
            live_symbol_benchmark = {}
        recent_windows.append(
            {
                "date": label,
                "daily_pnl_usdt": float(financial.get("daily_pnl_usdt", 0.0) or 0.0),
                "total_portfolio_value_usdt": float(financial.get("total_portfolio_value_usdt", 0.0) or 0.0),
                "carry_in_closed_count": int(loss_attribution.get("carry_in_closed_count", 0) or 0),
                "new_closed_count": int(loss_attribution.get("new_closed_count", 0) or 0),
                "primary_driver": str(loss_attribution.get("primary_driver", "") or "").strip(),
                "stagnation_exit_count": int(policy_exit_diagnostics.get("stagnation_exit_count", 0) or 0),
                "benchmark_candidate_id": str(live_symbol_benchmark.get("candidate_id", "") or "").strip(),
                "benchmark_expectancy_pct": float(live_symbol_benchmark.get("expectancy_pct", 0.0) or 0.0),
                "benchmark_profit_factor": float(live_symbol_benchmark.get("profit_factor", 0.0) or 0.0),
            }
        )

    previous_equity = float(settings.initial_balance_usdt or 0.0)
    for item in recent_windows:
        current_equity = float(item.get("total_portfolio_value_usdt", 0.0) or 0.0)
        item["equity_delta_usdt"] = current_equity - previous_equity if previous_equity > 0 and current_equity > 0 else 0.0
        previous_equity = current_equity if current_equity > 0 else previous_equity

    negative_day_count = sum(1 for item in recent_windows if float(item.get("equity_delta_usdt", 0.0) or 0.0) < 0)
    negative_streak = 0
    for item in reversed(recent_windows):
        if float(item.get("equity_delta_usdt", 0.0) or 0.0) < 0:
            negative_streak += 1
        else:
            break
    positive_streak = 0
    for item in reversed(recent_windows):
        if float(item.get("equity_delta_usdt", 0.0) or 0.0) > 0:
            positive_streak += 1
        else:
            break
    carry_in_loss_window_count = sum(
        1
        for item in recent_windows
        if int(item.get("carry_in_closed_count", 0) or 0) > 0 and float(item.get("daily_pnl_usdt", 0.0) or 0.0) < 0
    )
    carry_in_loss_streak = 0
    for item in reversed(recent_windows):
        if int(item.get("carry_in_closed_count", 0) or 0) > 0 and float(item.get("daily_pnl_usdt", 0.0) or 0.0) < 0:
            carry_in_loss_streak += 1
        else:
            break
    stagnation_exit_window_count = sum(
        1
        for item in recent_windows
        if int(item.get("stagnation_exit_count", 0) or 0) > 0 and float(item.get("daily_pnl_usdt", 0.0) or 0.0) <= 0
    )
    stagnation_exit_streak = 0
    for item in reversed(recent_windows):
        if int(item.get("stagnation_exit_count", 0) or 0) > 0 and float(item.get("daily_pnl_usdt", 0.0) or 0.0) <= 0:
            stagnation_exit_streak += 1
        else:
            break
    repeated_benchmark_leader_id = ""
    benchmark_leader_streak = 0
    for item in reversed(recent_windows):
        candidate_id = str(item.get("benchmark_candidate_id", "") or "").strip()
        benchmark_expectancy = float(item.get("benchmark_expectancy_pct", 0.0) or 0.0)
        benchmark_profit_factor = float(item.get("benchmark_profit_factor", 0.0) or 0.0)
        if (
            not candidate_id
            or candidate_id == "donchian_adx_perp_v1"
            or benchmark_expectancy <= 0.0
            or benchmark_profit_factor <= 1.0
        ):
            break
        if not repeated_benchmark_leader_id:
            repeated_benchmark_leader_id = candidate_id
            benchmark_leader_streak = 1
            continue
        if candidate_id == repeated_benchmark_leader_id:
            benchmark_leader_streak += 1
        else:
            break

    financial = daily_summary.get("financial_snapshot") or {}
    loss_attribution = daily_summary.get("loss_attribution") or {}
    current_equity_usdt = float(financial.get("total_portfolio_value_usdt", 0.0) or 0.0)
    configured_initial_usdt = float(settings.initial_balance_usdt or 0.0)
    multi_day_pnl_usdt = current_equity_usdt - configured_initial_usdt if configured_initial_usdt > 0 else 0.0
    drawdown_pct = (
        max(configured_initial_usdt - current_equity_usdt, 0.0) / configured_initial_usdt * 100.0
        if configured_initial_usdt > 0
        else 0.0
    )
    live_trade_expectancy_pct = float(loss_attribution.get("live_trade_expectancy_pct", 0.0) or 0.0)
    live_profit_factor = float(loss_attribution.get("live_profit_factor", 0.0) or 0.0)
    restore_equity_floor_usdt = configured_initial_usdt * float(
        settings.strategy_learning_restore_equity_recovery_ratio_pct or 0.0
    ) / 100.0
    previous_controls = (previous_memory or {}).get("controls", {})
    previous_mode = ""
    previous_cooldown_scale = None
    if isinstance(previous_controls, dict):
        previous_mode = str(previous_controls.get("fallback_entry_mode", "") or "").strip().lower()
        try:
            previous_cooldown_scale = float(previous_controls.get("cooldown_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            previous_cooldown_scale = None

    restore_ready = (
        positive_streak >= int(settings.strategy_learning_restore_positive_days or 0)
        and current_equity_usdt >= restore_equity_floor_usdt
    )
    force_fallback_base_only = bool(
        current_equity_usdt > 0
        and configured_initial_usdt > 0
        and current_equity_usdt < restore_equity_floor_usdt
        and negative_day_count >= int(settings.strategy_learning_negative_day_threshold or 0)
        and multi_day_pnl_usdt < 0
    )
    if previous_mode == "base_only" and not restore_ready:
        force_fallback_base_only = True
    previous_entry_mode = ""
    if isinstance(previous_controls, dict):
        previous_entry_mode = str(previous_controls.get("entry_mode", "") or "").strip().lower()

    capital_preservation_mode = bool(
        configured_initial_usdt > 0
        and drawdown_pct >= float(settings.strategy_learning_capital_preservation_drawdown_pct or 0.0)
        and negative_streak >= int(settings.strategy_learning_capital_preservation_negative_streak or 0)
        and live_trade_expectancy_pct < 0.0
        and live_profit_factor < 1.0
    )
    if previous_entry_mode == "capital_preservation" and not restore_ready:
        capital_preservation_mode = True

    preserve_cooldown_scale = None
    if previous_cooldown_scale is not None and previous_cooldown_scale < 1.0 and not restore_ready:
        preserve_cooldown_scale = previous_cooldown_scale

    live_symbol_benchmark = daily_summary.get("benchmark_watch_candidate_current") or {}
    if not isinstance(live_symbol_benchmark, dict) or not str(live_symbol_benchmark.get("candidate_id", "") or "").strip():
        top_by_symbol = (daily_summary.get("external_benchmarks") or {}).get("top_by_symbol") or {}
        live_symbol_benchmark = top_by_symbol.get(current_live_symbol, {}) if current_live_symbol else {}

    return {
        "lookback_days": lookback_days,
        "recent_windows": recent_windows,
        "negative_day_count": negative_day_count,
        "negative_streak": negative_streak,
        "positive_streak": positive_streak,
        "carry_in_loss_window_count": carry_in_loss_window_count,
        "carry_in_loss_streak": carry_in_loss_streak,
        "stagnation_exit_window_count": stagnation_exit_window_count,
        "stagnation_exit_streak": stagnation_exit_streak,
        "repeated_benchmark_leader_id": repeated_benchmark_leader_id,
        "benchmark_leader_streak": benchmark_leader_streak,
        "multi_day_pnl_usdt": round(multi_day_pnl_usdt, 4),
        "drawdown_pct": round(drawdown_pct, 4),
        "current_equity_usdt": round(current_equity_usdt, 4),
        "configured_initial_usdt": round(configured_initial_usdt, 4),
        "live_trade_expectancy_pct": round(live_trade_expectancy_pct, 4),
        "live_profit_factor": round(live_profit_factor, 4),
        "restore_positive_days": int(settings.strategy_learning_restore_positive_days or 0),
        "restore_equity_floor_usdt": round(restore_equity_floor_usdt, 4),
        "restore_ready": restore_ready,
        "force_fallback_base_only": force_fallback_base_only,
        "capital_preservation_mode": capital_preservation_mode,
        "preserve_cooldown_scale": preserve_cooldown_scale,
        "live_symbols": live_symbols,
        "current_live_symbol": current_live_symbol,
        "live_symbol_benchmark": live_symbol_benchmark if isinstance(live_symbol_benchmark, dict) else {},
        "previous_controls": previous_controls if isinstance(previous_controls, dict) else {},
        "current_date_label": current_date_label,
    }


def _current_report_window_start_epoch(now: datetime | None = None) -> float:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else datetime.now(LOCAL_TZ)
    anchor = local_now.replace(
        hour=int(REPORT_WINDOW_ANCHOR_HOUR_LOCAL),
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(timezone.utc).timestamp()


def _next_report_window_anchor(now: datetime | None = None) -> datetime:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else datetime.now(LOCAL_TZ)
    anchor = local_now.replace(
        hour=int(REPORT_WINDOW_ANCHOR_HOUR_LOCAL),
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now >= anchor:
        anchor += timedelta(days=1)
    return anchor


def _mark_trade_cooldown(path: Path, mode: str, symbol: str, cooldown_seconds: float) -> None:
    if cooldown_seconds <= 0:
        return
    cooldowns = _load_trade_cooldowns(path)
    cooldowns[_cooldown_key(mode, str(symbol))] = datetime.now(timezone.utc).timestamp() + cooldown_seconds
    _save_trade_cooldowns(path, cooldowns)


def _strategy_memory_controls(strategy_memory: dict | None) -> dict[str, object]:
    controls = (strategy_memory or {}).get("controls", {})
    return controls if isinstance(controls, dict) else {}


def _adaptive_trade_cooldown_seconds(report: dict, settings, strategy_memory: dict | None = None) -> float:
    base_cooldown = max(float(settings.trade_cooldown_seconds or 0.0), 0.0)
    if base_cooldown <= 0:
        return 0.0
    controls = _strategy_memory_controls(strategy_memory)
    try:
        cooldown_scale = float(controls.get("cooldown_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        cooldown_scale = 1.0
    base_cooldown *= max(0.25, min(cooldown_scale, 1.0))

    symbol_pool = report.get("symbol_pool")
    if isinstance(symbol_pool, list) and len(symbol_pool) == 1:
        single_symbol_cap = max(float(settings.trade_cooldown_single_symbol_cap_seconds or 0.0), 0.0)
        if single_symbol_cap > 0:
            base_cooldown = min(base_cooldown, single_symbol_cap)

    order = report.get("order") or {}
    if bool(order.get("reduce_only")):
        return min(base_cooldown, max(float(settings.trade_cooldown_min_seconds or 0.0), 0.0))

    idea = report.get("idea") or {}
    strategy_research = report.get("strategy_research") or {}
    llm_wake = report.get("llm_wake") or {}
    metrics = llm_wake.get("metrics") or {}

    action = str(idea.get("action", "") or "").lower()
    current_signal = str(strategy_research.get("current_signal", "hold") or "hold").lower()
    momentum_pct = abs(float(metrics.get("momentum_pct", 0.0) or 0.0))
    trade_delta_ratio = float(metrics.get("trade_delta_ratio", 0.0) or 0.0)
    volume_ratio = float(metrics.get("volume_ratio", 0.0) or 0.0)

    strong_long_follow_through = (
        action == "buy"
        and current_signal == "long"
        and momentum_pct >= float(settings.trade_cooldown_reentry_momentum_pct or 0.0)
        and trade_delta_ratio >= float(settings.trade_cooldown_reentry_trade_delta_ratio or 0.0)
        and volume_ratio >= float(settings.trade_cooldown_reentry_volume_ratio or 0.0)
    )
    strong_short_follow_through = (
        action == "sell"
        and current_signal == "short"
        and momentum_pct >= float(settings.trade_cooldown_reentry_momentum_pct or 0.0)
        and trade_delta_ratio <= -float(settings.trade_cooldown_reentry_trade_delta_ratio or 0.0)
        and volume_ratio >= float(settings.trade_cooldown_reentry_volume_ratio or 0.0)
    )
    if strong_long_follow_through or strong_short_follow_through:
        return max(
            max(float(settings.trade_cooldown_min_seconds or 0.0), 0.0),
            base_cooldown * max(float(settings.trade_cooldown_trend_multiplier or 0.0), 0.0),
    )
    return base_cooldown


def _apply_strategy_memory_fallback_policy(
    *,
    idea: TradeIdea,
    strategy_research,
    position_side: str,
    mode: str,
    strategy_memory: dict | None,
) -> tuple[TradeIdea, str]:
    controls = _strategy_memory_controls(strategy_memory)
    fallback_entry_mode = str(controls.get("fallback_entry_mode", "normal") or "normal").strip().lower()
    if fallback_entry_mode != "base_only":
        return idea, ""
    action = str(getattr(idea, "action", "hold") or "hold").lower()
    if action not in {"buy", "sell"}:
        return idea, ""
    if not _opens_new_exposure(action, position_side=position_side, mode=mode):
        return idea, ""
    current_signal = str(getattr(strategy_research, "current_signal", "hold") or "hold").lower()
    if current_signal != "hold":
        return idea, ""
    guarded = TradeIdea(
        action="hold",
        score=min(float(getattr(idea, "score", 0.40) or 0.40), 0.45),
        rationale=(
            f"{idea.rationale}; converted to hold because strategy memory disabled new fallback entries "
            f"after a loss window dominated by fallback trades"
        ),
        invalidation="wait for base-strategy alignment or the next reflection window",
        holding_horizon="none",
    )
    return guarded, "strategy-memory guard: fallback new entries disabled for this 12h window after fallback-led losses"


def _apply_strategy_memory_entry_policy(
    *,
    idea: TradeIdea,
    strategy_research,
    position_side: str,
    mode: str,
    strategy_memory: dict | None,
) -> tuple[TradeIdea, str]:
    controls = _strategy_memory_controls(strategy_memory)
    entry_mode = str(controls.get("entry_mode", "normal") or "normal").strip().lower()
    if entry_mode == "normal":
        return idea, ""
    action = str(getattr(idea, "action", "hold") or "hold").lower()
    if action not in {"buy", "sell"}:
        return idea, ""
    if not _opens_new_exposure(action, position_side=position_side, mode=mode):
        return idea, ""
    if entry_mode == "capital_preservation_pilot":
        pilot_candidate_id = str(controls.get("pilot_candidate_id", "") or "").strip()
        selected_strategy_id = str(getattr(strategy_research, "selected_strategy_id", "") or "").strip()
        execution_profile = getattr(strategy_research, "selected_execution_profile", {}) or {}
        entry_order_type = str(execution_profile.get("entry_order_type", "market") or "market").strip().lower()
        entry_liquidity = str(execution_profile.get("entry_liquidity", "taker") or "taker").strip().lower()
        if (
            pilot_candidate_id
            and selected_strategy_id == pilot_candidate_id
            and entry_order_type == "limit"
            and entry_liquidity == "maker"
        ):
            piloted = TradeIdea(
                action=idea.action,
                score=idea.score,
                rationale=(
                    f"{idea.rationale}; allowed under capital-preservation pilot mode because "
                    f"`{pilot_candidate_id}` has sustained post-cost benchmark support"
                ),
                invalidation=(
                    "cancel the pilot if maker-style fills degrade, benchmark expectancy turns negative, "
                    "or the next reflection window disables pilot mode"
                ),
                holding_horizon=idea.holding_horizon,
            )
            return piloted, ""
        guarded = TradeIdea(
            action="hold",
            score=min(float(getattr(idea, "score", 0.40) or 0.40), 0.45),
            rationale=(
                f"{idea.rationale}; converted to hold because capital-preservation pilot mode only allows "
                "maker-style entries for the approved pilot candidate"
            ),
            invalidation="wait for pilot candidate alignment or the next reflection window",
            holding_horizon="none",
        )
        return guarded, "strategy-memory guard: capital-preservation pilot only allows bounded maker entries for the approved candidate"
    if entry_mode != "capital_preservation":
        return idea, ""
    guarded = TradeIdea(
        action="hold",
        score=min(float(getattr(idea, "score", 0.40) or 0.40), 0.45),
        rationale=(
            f"{idea.rationale}; converted to hold because strategy memory activated capital-preservation mode "
            f"after a multi-window drawdown with negative live expectancy"
        ),
        invalidation="wait for shadow benchmark promotion or a later reflection window with clear recovery evidence",
        holding_horizon="none",
    )
    return guarded, "strategy-memory guard: capital-preservation mode disabled new live entries after a sustained drawdown"


def _opens_new_exposure(action: str, *, position_side: str, mode: str) -> bool:
    perp_mode = "perp" in mode
    if action == "buy":
        return not perp_mode or position_side != "short"
    if action == "sell":
        return perp_mode and position_side != "long"
    return False


def _derive_decision_source(
    *,
    idea: TradeIdea,
    strategy_research,
    policy_exit: bool,
    position_side: str,
    mode: str,
    guard_applied: bool = False,
    memory_guard_applied: bool = False,
) -> str:
    if policy_exit:
        return "policy_exit"
    if memory_guard_applied:
        return "memory_guard"
    if guard_applied:
        return "fallback_guard"

    current_signal = str(getattr(strategy_research, "current_signal", "hold") or "hold").lower()
    action = str(getattr(idea, "action", "hold") or "hold").lower()
    if action == "hold":
        return "base_strategy" if current_signal == "hold" else "fallback"
    if current_signal == "long" and action == "buy":
        return "base_strategy"
    if current_signal == "short" and action == "sell":
        return "base_strategy"
    if not _opens_new_exposure(action, position_side=position_side, mode=mode):
        return "base_strategy"
    return "fallback"


def _guard_range_fallback_override(
    *,
    idea: TradeIdea,
    strategy_research,
    llm_wake: dict,
    position_side: str,
    mode: str,
    settings,
) -> tuple[TradeIdea, str]:
    if not bool(settings.fallback_range_guard_enabled):
        return idea, ""
    action = str(getattr(idea, "action", "hold") or "hold").lower()
    if action not in {"buy", "sell"}:
        return idea, ""
    if not _opens_new_exposure(action, position_side=position_side, mode=mode):
        return idea, ""

    current_signal = str(getattr(strategy_research, "current_signal", "hold") or "hold").lower()
    current_signal_type = str(getattr(strategy_research, "current_signal_type", "hold") or "hold").lower()
    current_adx = float(getattr(strategy_research, "current_adx", 0.0) or 0.0)
    current_volume_ratio = float(getattr(strategy_research, "current_volume_ratio", 0.0) or 0.0)
    metrics = llm_wake.get("metrics") or {}
    volume_ratio = max(current_volume_ratio, float(metrics.get("volume_ratio", 0.0) or 0.0))
    trade_delta_ratio = float(metrics.get("trade_delta_ratio", 0.0) or 0.0)

    if current_signal != "hold":
        return idea, ""
    if current_signal_type not in {"hold", ""}:
        return idea, ""
    if current_adx > float(settings.fallback_range_guard_adx_max or 0.0):
        return idea, ""

    compelling_buy = (
        action == "buy"
        and trade_delta_ratio >= float(settings.fallback_range_guard_trade_delta_ratio or 0.0)
        and volume_ratio >= float(settings.fallback_range_guard_volume_ratio or 0.0)
    )
    compelling_sell = (
        action == "sell"
        and trade_delta_ratio <= -float(settings.fallback_range_guard_trade_delta_ratio or 0.0)
        and volume_ratio >= float(settings.fallback_range_guard_volume_ratio or 0.0)
    )
    if compelling_buy or compelling_sell:
        return idea, ""

    guarded = TradeIdea(
        action="hold",
        score=min(float(getattr(idea, "score", 0.40) or 0.40), 0.45),
        rationale=(
            f"{idea.rationale}; converted to hold because base strategy is neutral "
            f"(current_signal=hold, ADX {current_adx:.2f}) and fallback {action} lacked strong follow-through"
        ),
        invalidation="wait for a fresh directional setup or stronger tape confirmation",
        holding_horizon="none",
    )
    return guarded, f"neutral-base guard: ADX {current_adx:.2f}, volume {volume_ratio:.2f}x, trade_delta {trade_delta_ratio:+.2f}"


def _guard_fallback_open_exposure(
    *,
    idea: TradeIdea,
    strategy_research,
    llm_wake: dict,
    position_side: str,
    mode: str,
    settings,
) -> tuple[TradeIdea, str]:
    if not bool(settings.fallback_entry_guard_enabled):
        return idea, ""
    action = str(getattr(idea, "action", "hold") or "hold").lower()
    if action not in {"buy", "sell"}:
        return idea, ""
    if not _opens_new_exposure(action, position_side=position_side, mode=mode):
        return idea, ""

    current_signal = str(getattr(strategy_research, "current_signal", "hold") or "hold").lower()
    if (action == "buy" and current_signal == "long") or (action == "sell" and current_signal == "short"):
        return idea, ""

    metrics = llm_wake.get("metrics") or {}
    score = float(getattr(idea, "score", 0.0) or 0.0)
    momentum_pct = abs(float(metrics.get("momentum_pct", 0.0) or 0.0))
    volume_ratio = max(
        float(getattr(strategy_research, "current_volume_ratio", 0.0) or 0.0),
        float(metrics.get("volume_ratio", 0.0) or 0.0),
    )
    trade_delta_ratio = float(metrics.get("trade_delta_ratio", 0.0) or 0.0)
    required_score = float(settings.fallback_entry_min_score or 0.0)
    required_momentum = float(settings.fallback_entry_min_momentum_pct or 0.0)
    required_volume = float(settings.fallback_entry_min_volume_ratio or 0.0)
    required_trade_delta = float(settings.fallback_entry_min_trade_delta_ratio or 0.0)

    directional_delta_ok = (
        (action == "buy" and trade_delta_ratio >= required_trade_delta)
        or (action == "sell" and trade_delta_ratio <= -required_trade_delta)
    )
    strong_enough = (
        score >= required_score
        and momentum_pct >= required_momentum
        and volume_ratio >= required_volume
        and directional_delta_ok
    )
    if strong_enough:
        return idea, ""

    rationale = (
        f"{idea.rationale}; converted to hold because fallback open-entry thresholds were not met "
        f"(score {score:.2f}/{required_score:.2f}, momentum {momentum_pct:.2f}%/{required_momentum:.2f}%, "
        f"volume {volume_ratio:.2f}x/{required_volume:.2f}x, trade_delta {trade_delta_ratio:+.2f})"
    )
    guarded = TradeIdea(
        action="hold",
        score=min(score if score > 0 else 0.40, 0.45),
        rationale=rationale,
        invalidation="wait for base-strategy alignment or a stronger continuation-quality fallback setup",
        holding_horizon="none",
    )
    return guarded, (
        "fallback-entry guard: "
        f"base={current_signal}, score={score:.2f}, momentum={momentum_pct:.2f}%, "
        f"volume={volume_ratio:.2f}x, trade_delta={trade_delta_ratio:+.2f}"
    )


def _load_position_policy_state(path: Path) -> dict[str, dict]:
    raw = _read_json_file(path)
    return raw if isinstance(raw, dict) else {}


def _save_position_policy_state(path: Path, state: dict[str, dict]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _timeframe_to_minutes(timeframe: str) -> float:
    value = str(timeframe).strip().lower()
    if not value:
        return 0.0
    if value.endswith("m"):
        return float(value[:-1] or 0.0)
    if value.endswith("h"):
        return float(value[:-1] or 0.0) * 60.0
    if value.endswith("d"):
        return float(value[:-1] or 0.0) * 1440.0
    return 0.0


def _position_policy_key(mode: str, symbol: str) -> str:
    return f"{mode}:{symbol}"


def _is_same_direction_entry(position_side: str, action: str) -> bool:
    side = str(position_side or "").strip().lower()
    act = str(action or "").strip().lower()
    return (side == "long" and act == "buy") or (side == "short" and act == "sell")


def _record_position_policy_entry_fill(
    state: dict[str, dict],
    *,
    mode: str,
    symbol: str,
    position_side: str,
    entry_price: float,
    net_position: float,
    now_epoch: float,
) -> int:
    key = _position_policy_key(mode, symbol)
    if position_side not in {"long", "short"} or abs(net_position) <= 0:
        state.pop(key, None)
        return 0

    existing = state.get(key, {})
    same_episode = str(existing.get("position_side", "")).strip().lower() == position_side
    opened_at = float(existing.get("opened_at_epoch", now_epoch) or now_epoch) if same_episode else now_epoch
    entry_count = int(existing.get("entry_count", 0) or 0) + 1 if same_episode else 1
    state[key] = {
        "position_side": position_side,
        "entry_price": round(entry_price, 6),
        "opened_at_epoch": opened_at,
        "net_position": round(net_position, 8),
        "updated_at_epoch": now_epoch,
        "entry_count": entry_count,
        "last_entry_epoch": now_epoch,
    }
    return entry_count


def _sync_position_policy_state(
    state: dict[str, dict],
    *,
    mode: str,
    symbol: str,
    account,
    now_epoch: float,
    timeframe: str,
) -> dict[str, float | str | bool]:
    key = _position_policy_key(mode, symbol)
    position_side = str(getattr(account, "position_side", "flat"))
    entry_price = round(float(getattr(account, "entry_price", 0.0) or 0.0), 6)
    net_position = round(float(getattr(account, "net_position", 0.0) or 0.0), 8)
    timeframe_minutes = _timeframe_to_minutes(timeframe)
    if position_side not in {"long", "short"} or abs(net_position) <= 0:
        state.pop(key, None)
        return {
            "is_open": False,
            "position_side": "flat",
            "hold_minutes": 0.0,
            "hold_bars": 0.0,
        }

    entry = state.get(key, {})
    same_position = str(entry.get("position_side", "")).strip().lower() == position_side
    opened_at = float(entry.get("opened_at_epoch", now_epoch) or now_epoch) if same_position else now_epoch
    entry_count = int(entry.get("entry_count", 1) or 1) if same_position else 1
    state[key] = {
        "position_side": position_side,
        "entry_price": entry_price,
        "opened_at_epoch": opened_at,
        "net_position": net_position,
        "updated_at_epoch": now_epoch,
        "entry_count": entry_count,
        "last_entry_epoch": float(entry.get("last_entry_epoch", now_epoch) or now_epoch) if same_position else now_epoch,
    }
    hold_minutes = max((now_epoch - opened_at) / 60.0, 0.0)
    hold_bars = hold_minutes / timeframe_minutes if timeframe_minutes > 0 else 0.0
    return {
        "is_open": True,
        "position_side": position_side,
        "hold_minutes": round(hold_minutes, 2),
        "hold_bars": round(hold_bars, 2),
        "opened_at_epoch": opened_at,
        "opened_at_local": datetime.fromtimestamp(opened_at, tz=timezone.utc).astimezone(LOCAL_TZ).isoformat(),
        "entry_count": entry_count,
    }


def _daily_review_already_published(state_path: Path, date_label: str) -> bool:
    state = _read_json_file(state_path)
    return str(state.get("date_label", "")) == date_label and bool(state.get("page_id"))


def _load_runner_heartbeat(storage) -> dict[str, str]:
    if not storage.runner_log.exists():
        return {"text": "No monitor heartbeat yet", "timestamp": ""}

    latest_timestamp = ""
    latest_detail = ""
    try:
        for line in storage.runner_log.read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("event") != "monitor":
                continue
            latest_timestamp = str(payload.get("timestamp", latest_timestamp))
            latest_detail = str(payload.get("detail", latest_detail))
    except Exception:
        return {"text": "Unable to read runner heartbeat", "timestamp": ""}

    if not latest_timestamp:
        return {"text": "No monitor heartbeat yet", "timestamp": ""}
    return {"text": f"{latest_timestamp} ({latest_detail or 'runner heartbeat'})", "timestamp": latest_timestamp}


def _record_stage_metric(stage_metrics: dict[str, dict[str, float]], stage: str, elapsed_seconds: float) -> None:
    entry = stage_metrics.setdefault(stage, {"total_seconds": 0.0, "runs": 0.0})
    entry["total_seconds"] += max(elapsed_seconds, 0.0)
    entry["runs"] += 1.0


def _serialize_stage_metrics(stage_metrics: dict[str, dict[str, float]]) -> dict[str, dict[str, float | int]]:
    return {
        stage: {
            "total_seconds": round(float(metrics.get("total_seconds", 0.0)), 4),
            "runs": int(metrics.get("runs", 0.0)),
            "avg_seconds": round(
                float(metrics.get("total_seconds", 0.0)) / max(float(metrics.get("runs", 0.0)), 1.0),
                4,
            ),
        }
        for stage, metrics in stage_metrics.items()
        if float(metrics.get("runs", 0.0)) > 0
    }


def _normalize_dust_position(
    *,
    base_asset: float,
    last_price: float,
    min_order_value_usdt: float,
    dust_position_multiplier: float,
) -> tuple[float, dict[str, float | bool]]:
    dust_notional = max(base_asset, 0.0) * max(last_price, 0.0)
    dust_threshold = max(min_order_value_usdt * max(dust_position_multiplier, 0.0), 0.0)
    if base_asset > 0 and dust_threshold > 0 and dust_notional < dust_threshold:
        return 0.0, {
            "is_dust": True,
            "dust_notional_usdt": round(dust_notional, 4),
            "dust_threshold_usdt": round(dust_threshold, 4),
        }
    return base_asset, {
        "is_dust": False,
        "dust_notional_usdt": round(dust_notional, 4),
        "dust_threshold_usdt": round(dust_threshold, 4),
    }


def _pct(value: float) -> float:
    return value * 100.0


def _perp_liquidation_buffer_pct(mark_price: float, liq_price: float) -> float:
    if mark_price <= 0 or liq_price <= 0:
        return 0.0
    return abs((mark_price - liq_price) / mark_price) * 100.0


def _perp_position_return_pct(account) -> float:
    position_side = str(getattr(account, "position_side", "flat"))
    entry_price = float(getattr(account, "entry_price", 0.0) or 0.0)
    mark_price = float(getattr(account, "mark_price", entry_price) or entry_price)
    if position_side not in {"long", "short"} or entry_price <= 0 or mark_price <= 0:
        return 0.0
    if position_side == "long":
        return ((mark_price - entry_price) / entry_price) * 100.0
    return ((entry_price - mark_price) / entry_price) * 100.0


def _tighten_stop_loss(position_side: str, current_stop: float, candidate_stop: float) -> float:
    if candidate_stop <= 0:
        return max(current_stop, 0.0)
    if current_stop <= 0:
        return candidate_stop
    if position_side == "short":
        return min(current_stop, candidate_stop)
    return max(current_stop, candidate_stop)


def _tighten_take_profit(position_side: str, current_take_profit: float, candidate_take_profit: float) -> float:
    if candidate_take_profit <= 0:
        return max(current_take_profit, 0.0)
    if current_take_profit <= 0:
        return candidate_take_profit
    if position_side == "short":
        return max(current_take_profit, candidate_take_profit)
    return min(current_take_profit, candidate_take_profit)


def _snapshot_atr_pct(snapshot, period: int = 14) -> float:
    highs = [float(item) for item in getattr(snapshot, "highs", []) if float(item) > 0]
    lows = [float(item) for item in getattr(snapshot, "lows", []) if float(item) > 0]
    closes = [float(item) for item in getattr(snapshot, "closes", []) if float(item) > 0]
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return 0.0
    true_ranges: list[float] = []
    start = max(1, len(closes) - period)
    for idx in range(start, len(closes)):
        prev_close = closes[idx - 1]
        high = highs[idx]
        low = lows[idx]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not true_ranges:
        return 0.0
    last_price = closes[-1]
    if last_price <= 0:
        return 0.0
    return (fmean(true_ranges) / last_price) * 100.0


def _resolve_intraday_protection_profile(snapshot) -> dict[str, float | str]:
    if snapshot is None:
        return {
            "regime": "normal",
            "atr_pct": 0.0,
            "range_pct": 0.0,
            "net_move_pct": 0.0,
            "efficiency": 0.0,
            "stop_mult": 1.0,
            "take_mult": 1.0,
            "trigger1_mult": 1.0,
            "offset1_mult": 1.0,
            "trigger2_mult": 1.0,
            "lock2_mult": 1.0,
        }
    closes = [float(item) for item in getattr(snapshot, "closes", []) if float(item) > 0]
    highs = [float(item) for item in getattr(snapshot, "highs", []) if float(item) > 0]
    lows = [float(item) for item in getattr(snapshot, "lows", []) if float(item) > 0]
    if len(closes) < 15 or len(highs) < 15 or len(lows) < 15:
        return {
            "regime": "normal",
            "atr_pct": 0.0,
            "range_pct": 0.0,
            "net_move_pct": 0.0,
            "efficiency": 0.0,
            "stop_mult": 1.0,
            "take_mult": 1.0,
            "trigger1_mult": 1.0,
            "offset1_mult": 1.0,
            "trigger2_mult": 1.0,
            "lock2_mult": 1.0,
        }

    window = min(20, len(closes))
    window_high = max(highs[-window:])
    window_low = min(lows[-window:])
    last_price = closes[-1]
    range_pct = ((window_high - window_low) / last_price) * 100.0 if last_price > 0 else 0.0
    anchor_price = closes[-window]
    net_move_pct = abs(((closes[-1] - anchor_price) / anchor_price) * 100.0) if anchor_price > 0 else 0.0
    atr_pct = _snapshot_atr_pct(snapshot, period=min(14, len(closes) - 1))
    efficiency = net_move_pct / max(range_pct, 0.01)

    profile = {
        "regime": "normal",
        "atr_pct": round(atr_pct, 4),
        "range_pct": round(range_pct, 4),
        "net_move_pct": round(net_move_pct, 4),
        "efficiency": round(efficiency, 4),
        "stop_mult": 1.0,
        "take_mult": 1.0,
        "trigger1_mult": 1.0,
        "offset1_mult": 1.0,
        "trigger2_mult": 1.0,
        "lock2_mult": 1.0,
    }
    if atr_pct <= 0.85 and efficiency <= 0.35:
        profile.update(
            {
                "regime": "quiet_range",
                "stop_mult": 0.85,
                "take_mult": 0.80,
                "trigger1_mult": 0.75,
                "offset1_mult": 1.00,
                "trigger2_mult": 0.80,
                "lock2_mult": 0.85,
            }
        )
    elif efficiency >= 0.65 and net_move_pct >= 1.0:
        profile.update(
            {
                "regime": "directional_trend",
                "stop_mult": 0.95,
                "take_mult": 1.15,
                "trigger1_mult": 0.90,
                "offset1_mult": 1.00,
                "trigger2_mult": 0.90,
                "lock2_mult": 1.00,
            }
        )
    return profile


def _protection_targets_match(account, targets: dict[str, float], tolerance: float = 1e-4) -> bool:
    current_tp = float(getattr(account, "take_profit_price", 0.0) or 0.0)
    current_sl = float(getattr(account, "stop_loss_price", 0.0) or 0.0)
    current_trailing = float(getattr(account, "trailing_stop_distance", 0.0) or 0.0)
    return (
        abs(current_tp - float(targets.get("take_profit", 0.0))) <= tolerance
        and abs(current_sl - float(targets.get("stop_loss", 0.0))) <= tolerance
        and abs(current_trailing - float(targets.get("trailing_stop", 0.0))) <= tolerance
    )


def _build_perp_protection_targets(account, settings, snapshot=None) -> tuple[dict[str, float], dict[str, float | str]]:
    if getattr(account, "market_type", "spot") != "perp":
        return {"take_profit": 0.0, "stop_loss": 0.0, "trailing_stop": 0.0}, _resolve_intraday_protection_profile(snapshot)
    position_side = str(getattr(account, "position_side", "flat"))
    entry_price = float(getattr(account, "entry_price", 0.0) or 0.0)
    mark_price = float(getattr(account, "mark_price", entry_price) or entry_price)
    if position_side not in {"long", "short"} or entry_price <= 0:
        return {"take_profit": 0.0, "stop_loss": 0.0, "trailing_stop": 0.0}, _resolve_intraday_protection_profile(snapshot)

    profile = _resolve_intraday_protection_profile(snapshot)

    stop_pct = (max(float(settings.perp_hard_stop_loss_pct), 0.0) * float(profile["stop_mult"])) / 100.0
    take_pct = (max(float(settings.perp_take_profit_pct), 0.0) * float(profile["take_mult"])) / 100.0
    trail_pct = max(float(settings.perp_trailing_stop_pct), 0.0) / 100.0
    profit_pct = _perp_position_return_pct(account)
    current_take_profit = float(getattr(account, "take_profit_price", 0.0) or 0.0)
    current_stop_loss = float(getattr(account, "stop_loss_price", 0.0) or 0.0)

    if position_side == "long":
        stop_loss = entry_price * (1.0 - stop_pct) if stop_pct > 0 else 0.0
        take_profit = entry_price * (1.0 + take_pct) if take_pct > 0 else 0.0
    else:
        stop_loss = entry_price * (1.0 + stop_pct) if stop_pct > 0 else 0.0
        take_profit = entry_price * (1.0 - take_pct) if take_pct > 0 else 0.0

    take_profit = _tighten_take_profit(position_side, current_take_profit, take_profit)
    stop_loss = _tighten_stop_loss(position_side, current_stop_loss, stop_loss)

    trigger_1 = max(float(settings.perp_profit_lock_trigger_pct), 0.0) * float(profile["trigger1_mult"])
    breakeven_offset_pct = (max(float(settings.perp_profit_lock_breakeven_offset_pct), 0.0) * float(profile["offset1_mult"])) / 100.0
    trigger_2 = max(float(settings.perp_profit_lock_trigger_2_pct), 0.0) * float(profile["trigger2_mult"])
    lock_2_pct = (max(float(settings.perp_profit_lock_stop_2_pct), 0.0) * float(profile["lock2_mult"])) / 100.0

    if profit_pct >= trigger_1 and breakeven_offset_pct > 0:
        candidate_stop = (
            entry_price * (1.0 + breakeven_offset_pct)
            if position_side == "long"
            else entry_price * (1.0 - breakeven_offset_pct)
        )
        stop_loss = _tighten_stop_loss(position_side, stop_loss, candidate_stop)
    if profit_pct >= trigger_2 and lock_2_pct > 0:
        candidate_stop = (
            entry_price * (1.0 + lock_2_pct)
            if position_side == "long"
            else entry_price * (1.0 - lock_2_pct)
        )
        stop_loss = _tighten_stop_loss(position_side, stop_loss, candidate_stop)

    trailing_stop = mark_price * trail_pct if trail_pct > 0 and profit_pct >= trigger_1 else 0.0
    return {
        "take_profit": round(max(take_profit, 0.0), 6),
        "stop_loss": round(max(stop_loss, 0.0), 6),
        "trailing_stop": round(max(trailing_stop, 0.0), 6),
    }, profile


def _apply_perp_protection(exchange, symbol: str, settings, snapshot=None, *, force: bool = False) -> tuple[dict[str, float], dict, dict[str, float | str]]:
    if not settings.perp_enable_protection_orders:
        profile = _resolve_intraday_protection_profile(snapshot)
        return {"take_profit": 0.0, "stop_loss": 0.0, "trailing_stop": 0.0}, {"status": "disabled"}, profile
    account = None
    for _ in range(3):
        account = exchange.fetch_account_state(symbol)
        if getattr(account, "market_type", "spot") == "perp" and str(getattr(account, "position_side", "flat")) in {"long", "short"}:
            break
        sleep(0.6)
    if snapshot is None:
        try:
            snapshot = exchange.fetch_snapshot(symbol, settings.timeframe)
        except Exception:
            snapshot = None
    protection_targets, profile = _build_perp_protection_targets(account, settings, snapshot=snapshot) if account is not None else ({
        "take_profit": 0.0,
        "stop_loss": 0.0,
        "trailing_stop": 0.0,
    }, _resolve_intraday_protection_profile(snapshot))
    if not any(float(value) > 0 for value in protection_targets.values()):
        return protection_targets, {"status": "skipped", "reason": "no active perp position for protection"}, profile
    if not force and account is not None and _protection_targets_match(account, protection_targets):
        return protection_targets, {"status": "unchanged", "reason": "existing protection already matches target"}, profile
    protection_result = exchange.set_position_protection(symbol, **protection_targets)
    return protection_targets, protection_result, profile


def _market_wake_gate(snapshot, effective_base_asset: float, settings) -> dict:
    closes = [float(item) for item in snapshot.closes if float(item) > 0]
    volumes = [float(item) for item in snapshot.volumes if float(item) >= 0]
    if len(closes) < 21:
        return {
            "enabled": True,
            "score": 0,
            "required_score": settings.llm_wake_min_score,
            "reasons": ["not enough candles; allow LLM for safety"],
            "metrics": {},
        }

    recent_returns = [
        abs((closes[index] - closes[index - 1]) / closes[index - 1])
        for index in range(max(1, len(closes) - 6), len(closes))
        if closes[index - 1] > 0
    ]
    recent_volatility_pct = _pct(fmean(recent_returns)) if recent_returns else 0.0
    short_avg = fmean(closes[-5:])
    long_avg = fmean(closes[-20:])
    momentum_pct = _pct((short_avg - long_avg) / long_avg) if long_avg else 0.0
    recent_volume = fmean(volumes[-3:]) if len(volumes) >= 3 else 0.0
    baseline_volume = fmean(volumes[-20:]) if len(volumes) >= 20 else (fmean(volumes) if volumes else 0.0)
    volume_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
    rolling_high = max(closes[-20:])
    rolling_low = min(closes[-20:])
    high_distance_pct = _pct((rolling_high - closes[-1]) / rolling_high) if rolling_high else 999.0
    low_distance_pct = _pct((closes[-1] - rolling_low) / rolling_low) if rolling_low else 999.0
    breakout_proximity_pct = min(high_distance_pct, low_distance_pct)
    last_move_pct = _pct(abs((closes[-1] - closes[-2]) / closes[-2])) if len(closes) >= 2 and closes[-2] else 0.0
    depth_imbalance = abs(float(getattr(snapshot, "depth_imbalance", 0.0) or 0.0))
    trade_delta_ratio = abs(float(getattr(snapshot, "trade_delta_ratio", 0.0) or 0.0))
    large_trade_count = int(getattr(snapshot, "large_buy_count", 0) or 0) + int(
        getattr(snapshot, "large_sell_count", 0) or 0
    )

    score = 0
    core_signal_count = 0
    flow_signal_count = 0
    reasons: list[str] = []
    if recent_volatility_pct >= settings.llm_wake_volatility_pct:
        score += 1
        core_signal_count += 1
        reasons.append(f"volatility {recent_volatility_pct:.2f}%")
    if abs(momentum_pct) >= settings.llm_wake_momentum_pct:
        score += 1
        core_signal_count += 1
        reasons.append(f"momentum {momentum_pct:+.2f}%")
    if volume_ratio >= settings.llm_wake_volume_ratio:
        score += 1
        core_signal_count += 1
        reasons.append(f"volume {volume_ratio:.2f}x")
    if breakout_proximity_pct <= settings.llm_wake_breakout_proximity_pct:
        score += 1
        core_signal_count += 1
        reasons.append(f"near range edge {breakout_proximity_pct:.2f}%")
    if depth_imbalance >= settings.llm_wake_depth_imbalance:
        score += 1
        flow_signal_count += 1
        reasons.append(f"depth imbalance {depth_imbalance:.2f}")
    if trade_delta_ratio >= settings.llm_wake_trade_delta_ratio:
        score += 1
        flow_signal_count += 1
        reasons.append(f"trade delta {trade_delta_ratio:.2f}")
    if large_trade_count >= settings.llm_wake_large_trade_count:
        score += 1
        flow_signal_count += 1
        reasons.append(f"large prints {large_trade_count}")
    if effective_base_asset > 0 and last_move_pct >= settings.llm_wake_position_move_pct:
        score += 1
        reasons.append(f"held position moved {last_move_pct:.2f}%")

    required_score = settings.llm_wake_position_min_score if effective_base_asset > 0 else settings.llm_wake_min_score
    quiet_short_circuit = (
        effective_base_asset <= 0
        and recent_volatility_pct < float(settings.llm_wake_quiet_volatility_pct or 0.0)
        and abs(momentum_pct) < float(settings.llm_wake_momentum_pct or 0.0)
        and volume_ratio < float(settings.llm_wake_quiet_volume_ratio or 0.0)
        and breakout_proximity_pct > float(settings.llm_wake_breakout_proximity_pct or 0.0) * 1.5
        and flow_signal_count == 0
    )
    flow_only_suppressed = effective_base_asset <= 0 and core_signal_count == 0 and flow_signal_count > 0
    weak_core_signal_suppressed = (
        effective_base_asset <= 0
        and core_signal_count <= 1
        and score < max(required_score, 4)
        and breakout_proximity_pct > float(settings.llm_wake_breakout_proximity_pct or 0.0) * 1.25
        and recent_volatility_pct < max(float(settings.llm_wake_volatility_pct or 0.0), 0.30)
    )
    enabled = (not settings.llm_wake_gate_enabled) or score >= required_score
    if quiet_short_circuit:
        enabled = False
        reasons = ["quiet-market short-circuit"]
    elif flow_only_suppressed:
        enabled = False
        reasons = ["order-flow-only wake suppressed"]
    elif weak_core_signal_suppressed:
        enabled = False
        reasons = ["weak-setup short-circuit"]
    if not reasons:
        reasons.append("quiet market")
    return {
        "enabled": bool(enabled),
        "score": int(score),
        "required_score": float(required_score),
        "reasons": reasons,
        "metrics": {
            "recent_volatility_pct": round(recent_volatility_pct, 4),
            "momentum_pct": round(momentum_pct, 4),
            "volume_ratio": round(volume_ratio, 4),
            "breakout_proximity_pct": round(breakout_proximity_pct, 4),
            "last_move_pct": round(last_move_pct, 4),
            "depth_imbalance": round(float(getattr(snapshot, "depth_imbalance", 0.0) or 0.0), 4),
            "trade_delta_ratio": round(float(getattr(snapshot, "trade_delta_ratio", 0.0) or 0.0), 4),
            "large_trade_count": large_trade_count,
            "has_position": bool(effective_base_asset > 0),
            "core_signal_count": core_signal_count,
            "flow_signal_count": flow_signal_count,
        },
    }


def _intraday_policy_exit(
    *,
    snapshot,
    account,
    position_context: dict[str, float | str | bool],
    settings,
    strategy_memory: dict | None = None,
) -> TradeIdea | None:
    if getattr(account, "market_type", "spot") != "perp":
        return None
    position_side = str(getattr(account, "position_side", "flat"))
    if position_side not in {"long", "short"}:
        return None

    closes = [float(item) for item in getattr(snapshot, "closes", []) if float(item) > 0]
    if len(closes) < 20:
        return None

    hold_bars = float(position_context.get("hold_bars", 0.0) or 0.0)
    hold_minutes = float(position_context.get("hold_minutes", 0.0) or 0.0)
    opened_at_epoch = float(position_context.get("opened_at_epoch", 0.0) or 0.0)
    pnl_pct = _perp_position_return_pct(account)
    short_avg = fmean(closes[-5:])
    long_avg = fmean(closes[-20:])
    momentum_pct = ((short_avg - long_avg) / long_avg) * 100.0 if long_avg else 0.0
    last_move_pct = _pct(abs((closes[-1] - closes[-2]) / closes[-2])) if len(closes) >= 2 and closes[-2] else 0.0
    controls = _strategy_memory_controls(strategy_memory)
    try:
        hold_bars_scale = float(controls.get("hold_bars_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        hold_bars_scale = 1.0
    try:
        stagnation_bars_scale = float(controls.get("stagnation_bars_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        stagnation_bars_scale = 1.0
    try:
        stagnation_pnl_scale = float(controls.get("stagnation_pnl_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        stagnation_pnl_scale = 1.0
    hold_bars_scale = max(0.5, min(hold_bars_scale, 1.0))
    stagnation_bars_scale = max(0.5, min(stagnation_bars_scale, 1.0))
    stagnation_pnl_scale = max(0.75, min(stagnation_pnl_scale, 1.5))
    carry_in_mode = str(controls.get("carry_in_mode", "normal") or "normal").strip().lower()
    carry_in_position = opened_at_epoch > 0 and opened_at_epoch < _current_report_window_start_epoch()
    if carry_in_mode == "de_risk" and carry_in_position:
        hold_bars_scale = min(hold_bars_scale, 0.75)
        stagnation_bars_scale = min(stagnation_bars_scale, 0.75)
        stagnation_pnl_scale = max(stagnation_pnl_scale, 1.25)
    effective_stagnation_bars = float(settings.intraday_stagnation_bars) * stagnation_bars_scale
    effective_stagnation_pnl_pct = float(settings.intraday_stagnation_pnl_pct) * stagnation_pnl_scale
    effective_max_hold_bars = float(settings.intraday_max_hold_bars) * hold_bars_scale

    stale_trade = (
        hold_bars >= effective_stagnation_bars
        and abs(pnl_pct) <= effective_stagnation_pnl_pct
        and last_move_pct < 0.35
    )
    trend_has_reversed = (
        (position_side == "long" and momentum_pct <= -0.08)
        or (position_side == "short" and momentum_pct >= 0.08)
    )
    overheld_without_edge = (
        hold_bars >= effective_max_hold_bars
        and (pnl_pct <= 0.35 or trend_has_reversed)
    )

    now_local = datetime.now().astimezone()
    flatten_due = (
        bool(settings.intraday_force_flat_enabled)
        and (now_local.hour > int(settings.intraday_force_flat_hour_local)
             or (
                 now_local.hour == int(settings.intraday_force_flat_hour_local)
                 and now_local.minute >= int(settings.intraday_force_flat_minute_local)
             ))
        and (pnl_pct <= 0.6 or trend_has_reversed)
    )

    handoff_exit_due = False
    if carry_in_mode == "de_risk" and bool(getattr(settings, "intraday_report_handoff_exit_enabled", True)):
        next_anchor = _next_report_window_anchor(now_local)
        minutes_to_handoff = max((next_anchor - now_local).total_seconds() / 60.0, 0.0)
        handoff_window_minutes = max(float(getattr(settings, "intraday_report_handoff_exit_minutes", 30.0) or 30.0), 0.0)
        handoff_max_pnl_pct = float(getattr(settings, "intraday_report_handoff_max_pnl_pct", 0.45) or 0.45)
        weak_handoff_edge = (
            pnl_pct <= handoff_max_pnl_pct
            or trend_has_reversed
            or hold_bars >= max(1.5, effective_stagnation_bars * 0.5)
        )
        if minutes_to_handoff <= handoff_window_minutes and weak_handoff_edge:
            handoff_exit_due = True

    if not (stale_trade or overheld_without_edge or flatten_due or handoff_exit_due):
        return None

    if stale_trade:
        reason = (
            f"intraday stagnation exit after {hold_bars:.1f} bars / {hold_minutes:.0f}m; "
            f"pnl={pnl_pct:+.2f}% and price follow-through is weak"
        )
        if carry_in_mode == "de_risk" and carry_in_position:
            reason += "; carry-in de-risk mode tightened the exit window"
    elif handoff_exit_due:
        reason = (
            f"report-window handoff de-risk with {pnl_pct:+.2f}% pnl and {hold_bars:.1f} bars held; "
            "avoid carrying a weak position into the next noon window"
        )
    elif flatten_due:
        reason = (
            f"intraday end-of-day de-risk after {hold_bars:.1f} bars; "
            f"pnl={pnl_pct:+.2f}% and trend support is no longer strong enough"
        )
    else:
        reason = (
            f"intraday hold window exceeded ({hold_bars:.1f} bars / {hold_minutes:.0f}m); "
            f"pnl={pnl_pct:+.2f}% and momentum no longer justifies extension"
        )
        if carry_in_mode == "de_risk" and carry_in_position:
            reason += "; carry-in de-risk mode shortened the allowed hold window"

    return TradeIdea(
        action="sell" if position_side == "long" else "buy",
        score=0.91,
        rationale=reason,
        invalidation="policy-driven exit; reopen only on a fresh intraday setup",
        holding_horizon="exit-now",
    )


def _finalize_reporting(
    *,
    report: dict,
    storage,
    mode: str,
    progress: callable,
    settings,
    report_label: str,
    daily_reviewer: DailyReviewAgent,
    strategy_reflector: StrategyReflectionAgent,
) -> dict:
    progress("reporting", "running", report_label)
    external_benchmark_sync = {"status": "disabled", "reason": "external benchmark disabled"}
    if settings.external_benchmark_enabled:
        try:
            external_benchmark_sync = refresh_external_benchmark_suite(
                storage=storage,
                settings=settings,
                symbols=list(settings.observation_pool),
            )
        except Exception as exc:
            external_benchmark_sync = {"status": "error", "reason": str(exc)}
    report["external_benchmark_sync"] = external_benchmark_sync
    report["external_benchmarks"] = load_external_benchmark_summary(storage.external_benchmark_state)
    human_content = build_human_report(report, mode=mode, symbol=report["selected_symbol"])
    human_report_path = write_human_report(
        storage.reports,
        report["selected_symbol"],
        mode,
        human_content,
    )
    report["human_report"] = str(human_report_path)
    active_date_label = local_date_label()
    completed_date_label = completed_report_date_label()
    daily_content = build_daily_summary(storage.trade_logs, active_date_label, storage.runner_log)
    daily_report_path = write_daily_summary(storage.daily_reports, active_date_label, daily_content)
    report["daily_report"] = str(daily_report_path)
    daily_summary = load_daily_summary_data(storage.trade_logs, active_date_label, storage.runner_log)
    daily_summary["review_history"] = _load_recent_daily_review_history(storage, active_date_label)
    equity_history_path = mode_scoped_path(storage.equity_curve_history_state, mode)
    equity_chart_path = mode_scoped_path(storage.equity_curve_svg, mode)
    equity_curve = update_equity_curve(
        history_path=equity_history_path,
        chart_path=equity_chart_path,
        financial_snapshot=daily_summary.get("financial_snapshot", {}),
    )
    daily_summary["equity_curve"] = equity_curve
    report["equity_curve"] = equity_curve

    notion_sync = {"status": "disabled", "reason": "missing Notion token or status page id"}
    report["external_ai_review_sync"] = {"status": "disabled", "reason": "outside noon window or missing daily review"}
    if settings.notion_api_token and settings.notion_status_page_id:
        try:
            if report.get("cycle_mode") != "full" and "result" not in report:
                notion_sync = {
                    "status": "skipped",
                    "reason": "fast-cycle status sync deferred to heartbeat",
                    "mode": "heartbeat_deferred",
                }
            else:
                notion_sync = sync_notion_status(
                    token=settings.notion_api_token,
                    page_id=settings.notion_status_page_id,
                    page_title=settings.notion_status_page_title,
                    report=report,
                    daily_summary=daily_summary,
                    runner_heartbeat=_load_runner_heartbeat(storage),
                    lock_path=storage.notion_sync_lock,
                )
        except Exception as exc:
            notion_sync = {"status": "error", "reason": str(exc)}
    report["notion_sync"] = notion_sync

    daily_review_sync = {"status": "disabled", "reason": "outside noon window or missing runner heartbeat"}
    daily_strategy_review_path = _daily_strategy_review_path(storage, completed_date_label)
    external_review_path = external_ai_review_path(storage, completed_date_label)
    runner_heartbeat = _load_runner_heartbeat(storage)
    completed_daily_summary: dict | None = None
    if runner_heartbeat.get("timestamp") and datetime.now().astimezone().hour >= int(settings.notion_daily_review_hour):
        try:
            completed_daily_summary = load_daily_summary_data(storage.trade_logs, completed_date_label, storage.runner_log)
            completed_daily_summary["date_label"] = completed_date_label
            completed_daily_summary["review_history"] = _load_recent_daily_review_history(storage, completed_date_label)
            summary_fingerprint = _daily_review_fingerprint(completed_daily_summary)
            stored_review = _read_json_file(daily_strategy_review_path)
            if (
                not stored_review
                or stored_review.get("date_label") != completed_date_label
                or stored_review.get("summary_fingerprint") != summary_fingerprint
            ):
                daily_review = daily_reviewer.evaluate(completed_date_label, completed_daily_summary)
                stored_review = {
                    "date_label": completed_date_label,
                    "summary_fingerprint": summary_fingerprint,
                    **daily_review.__dict__,
                }
                _write_daily_strategy_review(daily_strategy_review_path, stored_review)
            daily_review_payload = {key: value for key, value in stored_review.items() if key != "date_label"}
            if _daily_review_already_published(storage.notion_daily_review_state, completed_date_label):
                state = _read_json_file(storage.notion_daily_review_state)
                daily_review_sync = {
                    "status": "skipped",
                    "reason": "daily review already published for this report window",
                    "page_id": state.get("page_id", ""),
                    "mode": "daily_review",
                }
            elif settings.notion_api_token and settings.notion_daily_review_parent_page_id:
                daily_review_sync = sync_notion_daily_review(
                    token=settings.notion_api_token,
                    parent_page_id=settings.notion_daily_review_parent_page_id,
                    date_label=completed_date_label,
                    page_title_prefix=settings.notion_daily_review_title_prefix,
                    daily_review=daily_review_payload,
                    daily_summary=completed_daily_summary,
                    state_path=storage.notion_daily_review_state,
                    lock_path=storage.notion_sync_lock,
                )
            else:
                daily_review_sync = {
                    "status": "stored",
                    "reason": "daily strategy review saved locally; Notion daily review is not configured",
                    "mode": "daily_review",
                }
        except Exception as exc:
            daily_review_sync = {"status": "error", "reason": str(exc)}

        external_ai_review_sync = {"status": "disabled", "reason": "external AI review disabled"}
        try:
            stored_external_review = load_external_ai_review(external_review_path)
            should_refresh_external_review = (
                not stored_external_review
                or stored_external_review.get("date_label") != completed_date_label
                or (
                    getattr(settings, "external_ai_review_enabled", False)
                    and str(stored_external_review.get("status", "")).lower() in {"disabled", "error"}
                )
            )
            if should_refresh_external_review:
                generated_external_review = generate_external_ai_review(
                    date_label=completed_date_label,
                    daily_summary=completed_daily_summary,
                    daily_review=daily_review_payload if 'daily_review_payload' in locals() else (stored_review or {}),
                    settings=settings,
                )
                stored_external_review = {"date_label": completed_date_label, **generated_external_review}
                save_external_ai_review(external_review_path, stored_external_review)
            external_ai_review_sync = {key: value for key, value in stored_external_review.items() if key != "date_label"}
        except Exception as exc:
            external_ai_review_sync = {"status": "error", "reason": str(exc)}
        report["external_ai_review_sync"] = external_ai_review_sync
    report["daily_review_sync"] = daily_review_sync
    if daily_strategy_review_path.exists():
        completed_daily_content = build_daily_summary(storage.trade_logs, completed_date_label, storage.runner_log)
        write_daily_summary(storage.daily_reports, completed_date_label, completed_daily_content)
        daily_content = build_daily_summary(storage.trade_logs, active_date_label, storage.runner_log)
        daily_report_path = write_daily_summary(storage.daily_reports, active_date_label, daily_content)
        report["daily_report"] = str(daily_report_path)
        daily_summary = load_daily_summary_data(storage.trade_logs, active_date_label, storage.runner_log)
        daily_summary["equity_curve"] = equity_curve

    strategy_memory_sync = {"status": "skipped", "reason": "12h reflection already up to date"}
    strategy_memory_updated = False
    try:
        current_slot = current_strategy_slot()
        strategy_memory = load_strategy_memory(storage.strategy_memory_state)
        controls_missing = not isinstance(strategy_memory.get("controls"), dict) or not strategy_memory.get("controls")
        if strategy_memory.get("slot") != current_slot or controls_missing:
            reflection_summary = completed_daily_summary or daily_summary
            reflection_context = _build_strategy_reflection_context(
                settings,
                storage,
                completed_date_label,
                reflection_summary,
                strategy_memory,
            )
            reflection = strategy_reflector.evaluate(current_slot, reflection_summary, reflection_context=reflection_context)
            payload = {
                "slot": reflection.slot,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "summary": reflection.summary,
                "biases": reflection.biases,
                "risk_adjustments": reflection.risk_adjustments,
                "focus_symbols": reflection.focus_symbols,
                "controls": reflection.controls,
                "reflection_context": reflection_context,
            }
            save_strategy_memory(storage.strategy_memory_state, payload)
            strategy_memory_updated = True
            strategy_memory_sync = {
                "status": "updated",
                "slot": current_slot,
                "reason": "backfilled controls" if controls_missing and strategy_memory.get("slot") == current_slot else "",
            }
    except Exception as exc:
        strategy_memory_sync = {"status": "error", "reason": str(exc)}
    report["strategy_memory_sync"] = strategy_memory_sync

    if strategy_memory_updated and completed_daily_summary is not None:
        try:
            completed_daily_summary = load_daily_summary_data(storage.trade_logs, completed_date_label, storage.runner_log)
            completed_daily_summary["date_label"] = completed_date_label
            completed_daily_summary["review_history"] = _load_recent_daily_review_history(storage, completed_date_label)
            refreshed_fingerprint = _daily_review_fingerprint(completed_daily_summary)
            refreshed_review = daily_reviewer.evaluate(completed_date_label, completed_daily_summary)
            stored_review = {
                "date_label": completed_date_label,
                "summary_fingerprint": refreshed_fingerprint,
                **refreshed_review.__dict__,
            }
            _write_daily_strategy_review(daily_strategy_review_path, stored_review)
            completed_daily_content = build_daily_summary(storage.trade_logs, completed_date_label, storage.runner_log)
            write_daily_summary(storage.daily_reports, completed_date_label, completed_daily_content)
            daily_content = build_daily_summary(storage.trade_logs, active_date_label, storage.runner_log)
            daily_report_path = write_daily_summary(storage.daily_reports, active_date_label, daily_content)
            report["daily_report"] = str(daily_report_path)
        except Exception as exc:
            report["strategy_memory_rebuild"] = {"status": "error", "reason": str(exc)}

    progress("reporting", "done", report_label)
    return report


def _parse_symbol_pool(symbol: str | None, settings) -> list[str]:
    if symbol:
        return [item.strip() for item in symbol.split(",") if item.strip()]
    if settings.observation_pool:
        return list(settings.observation_pool)
    return [settings.symbol]


def _build_exchange(mode: str, settings):
    if mode == "binance-testnet":
        return BinanceTestnetExchangeClient(
            api_key=settings.binance_testnet_api_key,
            secret=settings.binance_testnet_secret,
            microstructure_enabled=settings.market_microstructure_enabled,
            orderbook_depth_limit=settings.orderbook_depth_limit,
            recent_trade_limit=settings.recent_public_trade_limit,
        )
    if mode == "bybit-demo":
        return BybitDemoExchangeClient(
            api_key=settings.bybit_demo_api_key,
            secret=settings.bybit_demo_secret,
            microstructure_enabled=settings.market_microstructure_enabled,
            orderbook_depth_limit=settings.orderbook_depth_limit,
            recent_trade_limit=settings.recent_public_trade_limit,
            microstructure_cache_ttl_seconds=settings.microstructure_cache_ttl_seconds,
        )
    if mode == "bybit-demo-perp":
        return BybitDemoPerpExchangeClient(
            api_key=settings.bybit_demo_api_key,
            secret=settings.bybit_demo_secret,
            microstructure_enabled=settings.market_microstructure_enabled,
            orderbook_depth_limit=settings.orderbook_depth_limit,
            recent_trade_limit=settings.recent_public_trade_limit,
            microstructure_cache_ttl_seconds=settings.microstructure_cache_ttl_seconds,
        )
    return MockExchangeClient(
        initial_balance_usdt=settings.initial_balance_usdt,
        microstructure_enabled=settings.market_microstructure_enabled,
    )


def execute_cycle(
    mode: str,
    symbol: str | None = None,
    progress_callback: callable | None = None,
    cycle_mode: str = "full",
    cycle_reason: str = "",
) -> dict:
    def progress(stage: str, status: str, detail: str = "") -> None:
        if progress_callback is not None:
            progress_callback(stage, status, detail)

    stage_metrics: dict[str, dict[str, float]] = {}

    def timed_stage(stage: str, callback):
        started_at = perf_counter()
        result = callback()
        _record_stage_metric(stage_metrics, stage, perf_counter() - started_at)
        return result

    def build_trade_idea(payload: dict) -> TradeIdea:
        return TradeIdea(**payload)

    def build_sentiment_snapshot(payload: dict) -> SentimentSnapshot:
        return SentimentSnapshot(**payload)

    def build_backtest_snapshot(payload: dict) -> BacktestSnapshot:
        return BacktestSnapshot(**payload)

    def build_strategy_research_snapshot(payload: dict) -> StrategyResearchSnapshot:
        return StrategyResearchSnapshot(
            base_strategy_id=str(payload.get("base_strategy_id", "")),
            selected_strategy_id=str(payload.get("selected_strategy_id", "")),
            selected_strategy_name=str(payload.get("selected_strategy_name", "")),
            summary=str(payload.get("summary", "")),
            candidates=[
                StrategyCandidate(
                    strategy_id=str(item.get("strategy_id", "")),
                    name=str(item.get("name", "")),
                    source=str(item.get("source", "")),
                    credibility=str(item.get("credibility", "")),
                    description=str(item.get("description", "")),
                    backtest=build_backtest_snapshot(item.get("backtest", {})),
                )
                for item in payload.get("candidates", [])
                if isinstance(item, dict)
            ],
            selected_strategy_rationale=str(payload.get("selected_strategy_rationale", "")),
            selected_execution_profile=payload.get("selected_execution_profile", {}) if isinstance(payload.get("selected_execution_profile", {}), dict) else {},
            current_signal=str(payload.get("current_signal", "hold") or "hold"),
            current_signal_type=str(payload.get("current_signal_type", "hold") or "hold"),
            current_adx=float(payload.get("current_adx", 0.0) or 0.0),
            current_volume_ratio=float(payload.get("current_volume_ratio", 0.0) or 0.0),
        )

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    now_epoch = datetime.now(timezone.utc).timestamp()
    cooldowns = _load_trade_cooldowns(storage.trade_cooldown_state)
    position_policy_state = _load_position_policy_state(storage.position_policy_state)
    progress("setup", "running", "loading settings and models")
    llm_client = None
    if settings.model_backend == "ollama":
        llm_client = OllamaClient(
            host=settings.ollama_host,
            model=settings.model_name,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    analysis_llm_client = llm_client
    if cycle_mode != "full" and settings.llm_full_cycle_only:
        analysis_llm_client = None
    market_collector = MarketCollectorAgent()
    order_flow_collector = OrderFlowCollectorAgent()
    sentiment_provider = SentimentDataProvider(
        config_path=settings.sentiment_config_path,
        timeout_seconds=settings.sentiment_request_timeout_seconds,
        cache_ttl_seconds=settings.sentiment_cache_ttl_seconds,
        cache_state_path=storage.sentiment_http_cache_state,
    )
    sentiment_collector = SentimentCollectorAgent(provider=sentiment_provider)
    backtester = BacktestAgent()
    strategy_researcher = StrategyResearchAgent(settings.strategy_library_path, settings=settings, llm_client=analysis_llm_client)
    rule_strategy_researcher = StrategyResearchAgent(settings.strategy_library_path, settings=settings, llm_client=None)
    strategist = StrategistAgent(llm_client=analysis_llm_client)
    rule_strategist = StrategistAgent(llm_client=None)
    supervisor = RiskSupervisorAgent(llm_client=analysis_llm_client)
    rule_supervisor = RiskSupervisorAgent(llm_client=None)
    selector = SelectorAgent(llm_client=analysis_llm_client)
    executor = ExecutorAgent()
    evaluator = PostTradeEvaluatorAgent(llm_client=analysis_llm_client)
    daily_reviewer = DailyReviewAgent(llm_client=llm_client)
    strategy_reflector = StrategyReflectionAgent(llm_client=llm_client, settings=settings)
    strategy_memory = load_strategy_memory(storage.strategy_memory_state)

    exchange = _build_exchange(mode, settings)
    symbol_pool = _parse_symbol_pool(symbol, settings)
    progress("setup", "done", f"observation pool: {', '.join(symbol_pool)}")
    candidates: list[dict] = []

    for candidate_symbol in symbol_pool:
        progress("market_collector", "running", f"fetching {candidate_symbol} {settings.timeframe} market data")
        snapshot = timed_stage(
            "market_collector",
            lambda: exchange.fetch_snapshot(candidate_symbol, settings.timeframe),
        )
        account = exchange.fetch_account_state(candidate_symbol)
        protection_targets = {"take_profit": 0.0, "stop_loss": 0.0, "trailing_stop": 0.0}
        protection_profile: dict[str, float | str] = {}
        position_protection = {"status": "skipped", "reason": "no active perp position"}
        if mode == "bybit-demo-perp" and str(getattr(account, "position_side", "flat")) in {"long", "short"}:
            protection_started_at = perf_counter()
            protection_targets, position_protection, protection_profile = _apply_perp_protection(
                exchange,
                candidate_symbol,
                settings,
                snapshot,
                force=False,
            )
            _record_stage_metric(stage_metrics, "protection_sync", perf_counter() - protection_started_at)
            if str(position_protection.get("status", "")).lower() == "ok":
                account = exchange.fetch_account_state(candidate_symbol)
            else:
                protection_targets, protection_profile = _build_perp_protection_targets(account, settings, snapshot=snapshot)
        available_usdt = account.free_usdt
        actual_base_asset = account.base_asset
        position_side = getattr(account, "position_side", "flat")
        market_type = getattr(account, "market_type", "spot")
        position_context = _sync_position_policy_state(
            position_policy_state,
            mode=mode,
            symbol=candidate_symbol,
            account=account,
            now_epoch=now_epoch,
            timeframe=settings.timeframe,
        )
        min_order_value_usdt = 0.0
        if hasattr(exchange, "executable_min_order_value_usdt"):
            try:
                min_order_value_usdt = float(exchange.executable_min_order_value_usdt(candidate_symbol, float(snapshot.last_price)))
            except Exception:
                min_order_value_usdt = 0.0
        elif hasattr(exchange, "minimum_order_value_usdt"):
            try:
                min_order_value_usdt = float(exchange.minimum_order_value_usdt(candidate_symbol))
            except Exception:
                min_order_value_usdt = 0.0
        if market_type == "perp":
            available_base_asset = actual_base_asset
            dust_info = {
                "is_dust": False,
                "dust_notional_usdt": round(actual_base_asset * float(snapshot.last_price), 4),
                "dust_threshold_usdt": round(min_order_value_usdt, 4),
            }
        else:
            available_base_asset, dust_info = _normalize_dust_position(
                base_asset=actual_base_asset,
                last_price=float(snapshot.last_price),
                min_order_value_usdt=min_order_value_usdt,
                dust_position_multiplier=settings.dust_position_multiplier,
            )
        llm_wake = _market_wake_gate(snapshot, available_base_asset, settings)
        candidate_llm_client = analysis_llm_client if llm_wake["enabled"] else None
        candidate_strategy_researcher = strategy_researcher if candidate_llm_client is not None else rule_strategy_researcher
        candidate_strategist = strategist if candidate_llm_client is not None else rule_strategist
        candidate_supervisor = supervisor if candidate_llm_client is not None else rule_supervisor

        market_summary = f"{market_collector.summarize(snapshot)}; {order_flow_collector.summarize(snapshot)}"
        if dust_info.get("is_dust"):
            market_summary = (
                f"{market_summary}; dust position ignored for execution "
                f"({float(dust_info.get('dust_notional_usdt', 0.0)):.2f} < "
                f"{float(dust_info.get('dust_threshold_usdt', 0.0)):.2f} USDT)"
            )
        if not llm_wake["enabled"]:
            market_summary = (
                f"{market_summary}; LLM wake gate skipped "
                f"(score={llm_wake['score']}/{llm_wake['required_score']}, "
                f"{'; '.join(llm_wake['reasons'][:3])})"
            )
        progress("market_collector", "done", market_summary)
        progress("sentiment_collector", "running", f"collecting sentiment for {candidate_symbol}")
        sentiment_record = timed_stage(
            "sentiment_collector",
            lambda: sentiment_provider.collect(candidate_symbol),
        )
        sentiment = sentiment_record.snapshot
        sentiment_log_path = write_sentiment_record(storage.sentiment_data, sentiment_record)
        progress("sentiment_collector", "done", sentiment.summary)
        progress("backtester", "running", f"replaying recent candles for {candidate_symbol}")
        backtest = timed_stage(
            "backtester",
            lambda: backtester.evaluate(snapshot, sentiment),
        )
        progress("backtester", "done", backtest.summary)
        progress("strategy_researcher", "running", f"evaluating strategy library for {candidate_symbol}")
        strategy_research = timed_stage(
            "strategy_researcher",
            lambda: candidate_strategy_researcher.evaluate_with_memory(snapshot, sentiment, strategy_memory),
        )
        progress("strategy_researcher", "done", strategy_research.summary)
        policy_idea = _intraday_policy_exit(
            snapshot=snapshot,
            account=account,
            position_context=position_context,
            settings=settings,
            strategy_memory=strategy_memory,
        )
        progress(
            "strategist",
            "running",
            (
                f"generating trade idea for {candidate_symbol}"
                if llm_wake["enabled"]
                else f"using fallback idea for quiet {candidate_symbol}"
            ),
        )
        if policy_idea is not None:
            idea = policy_idea
            _record_stage_metric(stage_metrics, "strategist", 0.0)
        else:
            idea = timed_stage(
                "strategist",
                lambda: candidate_strategist.evaluate(
                    snapshot,
                    sentiment,
                    backtest,
                    strategy_research,
                    market_summary=market_summary,
                    available_usdt=available_usdt,
                    available_base_asset=available_base_asset,
                    position_side=position_side,
                    min_order_value_usdt=min_order_value_usdt,
                    aggressive_mode=settings.demo_aggressive_mode,
                    trading_mode=mode,
                    strategy_memory=strategy_memory,
                ),
            )
        idea, memory_guard_reason = _apply_strategy_memory_fallback_policy(
            idea=idea,
            strategy_research=strategy_research,
            position_side=position_side,
            mode=mode,
            strategy_memory=strategy_memory,
        )
        if not memory_guard_reason:
            idea, memory_guard_reason = _apply_strategy_memory_entry_policy(
                idea=idea,
                strategy_research=strategy_research,
                position_side=position_side,
                mode=mode,
                strategy_memory=strategy_memory,
            )
        idea, fallback_guard_reason = _guard_fallback_open_exposure(
            idea=idea,
            strategy_research=strategy_research,
            llm_wake=llm_wake,
            position_side=position_side,
            mode=mode,
            settings=settings,
        )
        if not fallback_guard_reason:
            idea, fallback_guard_reason = _guard_range_fallback_override(
                idea=idea,
                strategy_research=strategy_research,
                llm_wake=llm_wake,
                position_side=position_side,
                mode=mode,
                settings=settings,
            )
        else:
            # Keep a single guard reason in the report for attribution/reporting simplicity.
            fallback_guard_reason = str(fallback_guard_reason)
        decision_source = _derive_decision_source(
            idea=idea,
            strategy_research=strategy_research,
            policy_exit=bool(policy_idea is not None),
            position_side=position_side,
            mode=mode,
            guard_applied=bool(fallback_guard_reason),
            memory_guard_applied=bool(memory_guard_reason),
        )
        risk_feedback = ""
        progress("strategist", "done", f"{candidate_symbol}: {idea.action} ({idea.score:.2f})")
        progress("risk_supervisor", "running", f"reviewing {candidate_symbol}")
        use_candidate_llm_risk = (
            bool(candidate_llm_client)
            and cycle_mode == "full"
            and not settings.llm_selected_candidate_only
        )
        if use_candidate_llm_risk:
            risk_feedback = timed_stage(
                "risk_supervisor",
                lambda: candidate_supervisor.critique(
                    idea=idea,
                    sentiment=sentiment,
                    backtest=backtest,
                    strategy_research=strategy_research,
                    strategy_memory=strategy_memory,
                    use_llm=True,
                ),
            )
            if risk_feedback:
                progress("strategist", "running", f"revising {candidate_symbol} after risk critique")
                idea = timed_stage(
                    "strategist",
                    lambda: candidate_strategist.refine_with_risk_feedback(
                        idea,
                        risk_feedback,
                        available_usdt=available_usdt,
                        available_base_asset=available_base_asset,
                        position_side=position_side,
                        trading_mode=mode,
                        strategy_memory=strategy_memory,
                    ),
                )
                progress("strategist", "done", f"{candidate_symbol}: revised to {idea.action} ({idea.score:.2f})")
        approval = timed_stage(
            "risk_supervisor",
            lambda: candidate_supervisor.review(
                idea=idea,
                sentiment=sentiment,
                backtest=backtest,
                strategy_research=strategy_research,
                available_usdt=available_usdt,
                available_base_asset=available_base_asset,
                position_side=position_side,
                last_price=float(snapshot.last_price),
                min_order_value_usdt=min_order_value_usdt,
                min_signal_score=settings.min_signal_score,
                max_position_pct=settings.max_position_pct,
                trading_mode=mode,
                aggressive_mode=settings.demo_aggressive_mode,
                expectancy_floor_pct=settings.expectancy_floor_pct,
                taker_fee_pct=settings.taker_fee_pct,
                buy_balance_buffer_pct=settings.buy_balance_buffer_pct,
                fee_hurdle_multiplier=settings.fee_hurdle_multiplier,
                cycle_mode=cycle_mode,
                signal_boost=settings.fast_cycle_signal_boost if cycle_mode != "full" else 0.0,
                strategy_memory=strategy_memory,
                use_llm=use_candidate_llm_risk,
                total_equity_usdt=float(getattr(account, "total_equity_usdt", available_usdt)),
                current_position_notional_usdt=float(getattr(account, "position_notional_usdt", 0.0)),
                current_leverage=float(getattr(account, "leverage", 0.0)),
                liq_price=float(getattr(account, "liq_price", 0.0)),
                position_mm_usdt=float(getattr(account, "position_mm_usdt", 0.0)),
                perp_max_leverage=settings.perp_max_leverage,
                perp_min_available_balance_ratio_pct=settings.perp_min_available_balance_ratio_pct,
                perp_min_liquidation_buffer_pct=settings.perp_min_liquidation_buffer_pct,
            ),
        )
        episode_entry_count = int(position_context.get("entry_count", 0) or 0)
        max_entries_per_episode = max(int(settings.intraday_max_entries_per_episode or 0), 0)
        if (
            approval.approved
            and idea.action != "hold"
            and max_entries_per_episode > 0
            and _is_same_direction_entry(position_side, idea.action)
            and episode_entry_count >= max_entries_per_episode
        ):
            entry_warnings = list(approval.warnings)
            entry_warnings.append("same-direction add-on capped to reduce fee drag")
            approval = type(approval)(
                approved=False,
                reason=f"episode entry cap reached: {episode_entry_count}/{max_entries_per_episode}",
                max_notional_usdt=0.0,
                warnings=entry_warnings,
            )
        cooldown_remaining = _cooldown_remaining_seconds(cooldowns, mode, candidate_symbol, now_epoch)
        if idea.action != "hold" and cooldown_remaining > 0:
            cooldown_warnings = list(approval.warnings)
            cooldown_warnings.append("recent trade cooldown active to reduce fee bleed")
            approval = type(approval)(
                approved=False,
                reason=f"symbol cooldown active: {int(cooldown_remaining)}s remaining",
                max_notional_usdt=0.0,
                warnings=cooldown_warnings,
            )
        progress("risk_supervisor", "done", f"{candidate_symbol}: {approval.reason}")
        selected_strategy_backtest = next(
            (
                item.backtest.__dict__
                for item in strategy_research.candidates
                if item.strategy_id == strategy_research.selected_strategy_id
            ),
            backtest.__dict__,
        )
        candidates.append(
            {
                "symbol": candidate_symbol,
                "market_summary": market_summary,
                "last_price": snapshot.last_price,
                "sentiment_log": str(sentiment_log_path),
                "sentiment": sentiment.__dict__,
                "backtest": backtest.__dict__,
                "strategy_research": {
                    "base_strategy_id": strategy_research.base_strategy_id,
                    "selected_strategy_id": strategy_research.selected_strategy_id,
                    "selected_strategy_name": strategy_research.selected_strategy_name,
                    "selected_strategy_rationale": strategy_research.selected_strategy_rationale,
                    "selected_execution_profile": strategy_research.selected_execution_profile,
                    "current_signal": strategy_research.current_signal,
                    "current_signal_type": strategy_research.current_signal_type,
                    "current_adx": strategy_research.current_adx,
                    "current_volume_ratio": strategy_research.current_volume_ratio,
                    "summary": strategy_research.summary,
                    "candidates": [
                        {
                            "strategy_id": item.strategy_id,
                            "name": item.name,
                            "source": item.source,
                            "credibility": item.credibility,
                            "description": item.description,
                            "backtest": item.backtest.__dict__,
                        }
                        for item in strategy_research.candidates
                    ],
                },
                "selected_strategy_backtest": selected_strategy_backtest,
                "idea": idea.__dict__,
                "approval": approval.__dict__,
                "account": {
                    "free_usdt": round(available_usdt, 4),
                    "base_asset": round(actual_base_asset, 8),
                    "effective_base_asset": round(available_base_asset, 8),
                    "base_symbol": candidate_symbol.split("/")[0],
                    "market_type": market_type,
                    "position_side": position_side,
                    "net_position": round(float(getattr(account, "net_position", actual_base_asset)), 8),
                    "entry_price": round(float(getattr(account, "entry_price", 0.0)), 6),
                    "mark_price": round(float(getattr(account, "mark_price", snapshot.last_price)), 6),
                    "position_notional_usdt": round(float(getattr(account, "position_notional_usdt", actual_base_asset * float(snapshot.last_price))), 4),
                    "unrealized_pnl_usdt": round(float(getattr(account, "unrealized_pnl_usdt", 0.0)), 4),
                    "cum_realized_pnl_usdt": round(float(getattr(account, "cum_realized_pnl_usdt", 0.0)), 4),
                    "total_equity_usdt": round(float(getattr(account, "total_equity_usdt", available_usdt)), 4),
                    "available_balance_usdt": round(float(getattr(account, "available_balance_usdt", available_usdt)), 4),
                    "leverage": round(float(getattr(account, "leverage", 0.0)), 4),
                    "liq_price": round(float(getattr(account, "liq_price", 0.0)), 6),
                    "position_im_usdt": round(float(getattr(account, "position_im_usdt", 0.0)), 4),
                    "position_mm_usdt": round(float(getattr(account, "position_mm_usdt", 0.0)), 4),
                    "take_profit_price": round(float(getattr(account, "take_profit_price", 0.0)), 6),
                    "stop_loss_price": round(float(getattr(account, "stop_loss_price", 0.0)), 6),
                    "trailing_stop_distance": round(float(getattr(account, "trailing_stop_distance", 0.0)), 6),
                    "position_status": str(getattr(account, "position_status", "Normal")),
                    "is_reduce_only": bool(getattr(account, "is_reduce_only", False)),
                    "liquidation_buffer_pct": round(
                        _perp_liquidation_buffer_pct(
                            float(getattr(account, "mark_price", snapshot.last_price) or snapshot.last_price),
                            float(getattr(account, "liq_price", 0.0) or 0.0),
                        ),
                        4,
                    ),
                    "dust_position": bool(dust_info.get("is_dust")),
                    "dust_notional_usdt": round(float(dust_info.get("dust_notional_usdt", 0.0)), 4),
                    "hold_minutes": round(float(position_context.get("hold_minutes", 0.0)), 2),
                    "hold_bars": round(float(position_context.get("hold_bars", 0.0)), 2),
                    "opened_at_epoch": round(float(position_context.get("opened_at_epoch", 0.0) or 0.0), 3),
                    "opened_at_local": str(position_context.get("opened_at_local", "")),
                    "entry_count": int(position_context.get("entry_count", 0) or 0),
                },
                "execution_constraints": {
                    "min_order_value_usdt": round(min_order_value_usdt, 4),
                    "cooldown_remaining_seconds": int(cooldown_remaining),
                    "dust_threshold_usdt": round(float(dust_info.get("dust_threshold_usdt", 0.0)), 4),
                    "max_entries_per_episode": max_entries_per_episode,
                },
                "llm_wake": llm_wake,
                "protection_targets": protection_targets,
                "protection_profile": protection_profile,
                "protection_sync": position_protection,
                "decision_source": decision_source,
                "strategy_memory": {
                    "slot": str(strategy_memory.get("slot", "")),
                    "summary": str(strategy_memory.get("summary", "")),
                    "controls": dict(_strategy_memory_controls(strategy_memory)),
                },
                "debate": {
                    "risk_feedback": risk_feedback,
                    "fallback_guard_reason": fallback_guard_reason,
                    "memory_guard_reason": memory_guard_reason,
                },
                "position_context": position_context,
                "policy_exit": bool(policy_idea is not None),
            }
        )
    _save_position_policy_state(storage.position_policy_state, position_policy_state)

    progress("selector", "running", "ranking symbol candidates")
    selected, selection_summary = timed_stage(
        "selector",
        lambda: selector.select(candidates, strategy_memory=strategy_memory),
    )
    progress("selector", "done", selection_summary)

    if (
        bool(analysis_llm_client)
        and cycle_mode == "full"
        and settings.llm_selected_candidate_only
        and selected.get("llm_wake", {}).get("enabled", True)
        and selected.get("idea", {}).get("action") != "hold"
        and selected.get("approval", {}).get("approved")
        and not bool(selected.get("policy_exit"))
    ):
        selected_idea = build_trade_idea(selected["idea"])
        selected_sentiment = build_sentiment_snapshot(selected["sentiment"])
        selected_backtest = build_backtest_snapshot(selected["backtest"])
        selected_strategy_research = build_strategy_research_snapshot(selected["strategy_research"])
        progress("risk_supervisor", "running", f"selected-candidate debate for {selected['symbol']}")
        risk_feedback = timed_stage(
            "risk_supervisor",
            lambda: supervisor.critique(
                idea=selected_idea,
                sentiment=selected_sentiment,
                backtest=selected_backtest,
                strategy_research=selected_strategy_research,
                strategy_memory=strategy_memory,
                use_llm=True,
            ),
        )
        if risk_feedback:
            progress("strategist", "running", f"revising selected candidate {selected['symbol']}")
            revised_idea = timed_stage(
                "strategist",
                lambda: strategist.refine_with_risk_feedback(
                    selected_idea,
                    risk_feedback,
                    available_usdt=float(selected["account"]["free_usdt"]),
                    available_base_asset=float(selected["account"].get("effective_base_asset", selected["account"]["base_asset"])),
                    position_side=str(selected["account"].get("position_side", "flat")),
                    trading_mode=mode,
                    strategy_memory=strategy_memory,
                ),
            )
            selected["idea"] = revised_idea.__dict__
            selected_idea = revised_idea
            progress(
                "strategist",
                "done",
                f"{selected['symbol']}: revised to {selected['idea']['action']} ({float(selected['idea']['score']):.2f})",
            )
        selected_approval = timed_stage(
            "risk_supervisor",
            lambda: supervisor.review(
                idea=selected_idea,
                sentiment=selected_sentiment,
                backtest=selected_backtest,
                strategy_research=selected_strategy_research,
                available_usdt=float(selected["account"]["free_usdt"]),
                available_base_asset=float(selected["account"].get("effective_base_asset", selected["account"]["base_asset"])),
                position_side=str(selected["account"].get("position_side", "flat")),
                last_price=float(selected["last_price"]),
                min_order_value_usdt=float(selected["execution_constraints"].get("min_order_value_usdt", 0.0)),
                min_signal_score=settings.min_signal_score,
                max_position_pct=settings.max_position_pct,
                trading_mode=mode,
                aggressive_mode=settings.demo_aggressive_mode,
                expectancy_floor_pct=settings.expectancy_floor_pct,
                taker_fee_pct=settings.taker_fee_pct,
                buy_balance_buffer_pct=settings.buy_balance_buffer_pct,
                fee_hurdle_multiplier=settings.fee_hurdle_multiplier,
                cycle_mode=cycle_mode,
                signal_boost=0.0,
                strategy_memory=strategy_memory,
                use_llm=True,
                total_equity_usdt=float(selected["account"].get("total_equity_usdt", selected["account"]["free_usdt"])),
                current_position_notional_usdt=float(selected["account"].get("position_notional_usdt", 0.0)),
                current_leverage=float(selected["account"].get("leverage", 0.0)),
                liq_price=float(selected["account"].get("liq_price", 0.0)),
                position_mm_usdt=float(selected["account"].get("position_mm_usdt", 0.0)),
                perp_max_leverage=settings.perp_max_leverage,
                perp_min_available_balance_ratio_pct=settings.perp_min_available_balance_ratio_pct,
                perp_min_liquidation_buffer_pct=settings.perp_min_liquidation_buffer_pct,
            ),
        )
        selected["approval"] = selected_approval.__dict__
        selected["debate"] = {
            "risk_feedback": risk_feedback,
            "fallback_guard_reason": selected.get("debate", {}).get("fallback_guard_reason", ""),
            "memory_guard_reason": selected.get("debate", {}).get("memory_guard_reason", ""),
        }
        selected["decision_source"] = _derive_decision_source(
            idea=selected_idea,
            strategy_research=selected_strategy_research,
            policy_exit=bool(selected.get("policy_exit")),
            position_side=str(selected["account"].get("position_side", "flat")),
            mode=mode,
            guard_applied=bool(selected.get("debate", {}).get("fallback_guard_reason")),
            memory_guard_applied=bool(selected.get("debate", {}).get("memory_guard_reason")),
        )
        progress("risk_supervisor", "done", f"{selected['symbol']}: {selected_approval.reason}")

    report = {
        "mode": mode,
        "storage_root": str(storage.root),
        "symbol_pool": symbol_pool,
        "cycle_mode": cycle_mode,
        "cycle_reason": cycle_reason,
        "llm_enabled_for_cycle": bool(analysis_llm_client),
        "selection_summary": selection_summary,
        "selected_symbol": selected["symbol"],
        "candidates": candidates,
        "market_summary": selected["market_summary"],
        "last_price": selected["last_price"],
        "sentiment_log": selected["sentiment_log"],
        "sentiment": selected["sentiment"],
        "backtest": selected["backtest"],
        "strategy_research": selected["strategy_research"],
        "selected_strategy_backtest": selected.get("selected_strategy_backtest", selected["backtest"]),
        "idea": selected["idea"],
        "approval": selected["approval"],
        "account": selected["account"],
        "position_context": selected.get("position_context", {}),
        "execution_constraints": selected.get("execution_constraints", {}),
        "llm_wake": selected.get("llm_wake", {}),
        "decision_source": selected.get("decision_source", "unknown"),
        "strategy_memory": selected.get("strategy_memory", {}),
        "debate": selected.get("debate", {}),
        "stage_metrics": _serialize_stage_metrics(stage_metrics),
    }

    if not report["approval"]["approved"]:
        progress("executor", "skipped", "no executable order for this cycle")
        progress("post_trade_evaluator", "skipped", "no execution result to evaluate")
        trade_log_path = write_json_log(storage.trade_logs, "decision", report)
        report["trade_log"] = str(trade_log_path)
        return _finalize_reporting(
            report=report,
            storage=storage,
            mode=mode,
            progress=progress,
            settings=settings,
            report_label="decision saved",
            daily_reviewer=daily_reviewer,
            strategy_reflector=strategy_reflector,
        )

    progress("executor", "running", f"submitting {report['idea']['action']} order for {report['selected_symbol']}")
    executor_started_at = perf_counter()
    order = executor.build_order(
        symbol=report["selected_symbol"],
        side=report["idea"]["action"],
        notional_usdt=report["approval"]["max_notional_usdt"],
        price=float(report["last_price"]),
        available_usdt=float(report["account"]["free_usdt"]),
        available_base_asset=float(report["account"].get("effective_base_asset", report["account"]["base_asset"])),
        trading_mode=mode,
        position_side=str(report["account"].get("position_side", "flat")),
        buy_balance_buffer_pct=settings.buy_balance_buffer_pct,
        target_leverage=settings.perp_max_leverage if mode == "bybit-demo-perp" else 0.0,
        execution_profile=report["strategy_research"].get("selected_execution_profile", {}),
        market_snapshot=selected.get("snapshot"),
    )
    try:
        result = exchange.execute_order(order)
    except Exception as exc:
        result = {
            "status": "rejected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exchange_error": str(exc),
            "order": order,
        }
    _record_stage_metric(stage_metrics, "executor", perf_counter() - executor_started_at)
    report["stage_metrics"] = _serialize_stage_metrics(stage_metrics)
    progress("executor", "done", result.get("status", "submitted"))
    progress("post_trade_evaluator", "running", "evaluating trade outcome")
    evaluation = timed_stage(
        "post_trade_evaluator",
        lambda: evaluator.evaluate(selected["idea"], result),
    )
    report["stage_metrics"] = _serialize_stage_metrics(stage_metrics)
    progress("post_trade_evaluator", "done", evaluation.grade)
    report["order"] = order
    report["result"] = result
    report["evaluation"] = evaluation.__dict__
    if str(result.get("status", "")).lower() in {"accepted", "filled"} and mode == "bybit-demo-perp" and not bool(order.get("reduce_only")):
        try:
            protection_targets, protection_result, protection_profile = _apply_perp_protection(
                exchange,
                report["selected_symbol"],
                settings,
                selected.get("snapshot"),
            )
            report["protection_targets"] = protection_targets
            report["protection_profile"] = protection_profile
            report["protection_result"] = protection_result
            refreshed_account = exchange.fetch_account_state(report["selected_symbol"])
            report["account"].update(
                {
                    "free_usdt": round(float(getattr(refreshed_account, "free_usdt", report["account"]["free_usdt"])), 4),
                    "base_asset": round(float(getattr(refreshed_account, "base_asset", report["account"]["base_asset"])), 8),
                    "effective_base_asset": round(float(getattr(refreshed_account, "base_asset", report["account"].get("effective_base_asset", report["account"]["base_asset"]))), 8),
                    "position_side": str(getattr(refreshed_account, "position_side", report["account"].get("position_side", "flat"))),
                    "net_position": round(float(getattr(refreshed_account, "net_position", report["account"].get("net_position", 0.0))), 8),
                    "entry_price": round(float(getattr(refreshed_account, "entry_price", report["account"].get("entry_price", 0.0))), 6),
                    "mark_price": round(float(getattr(refreshed_account, "mark_price", report["account"].get("mark_price", report["last_price"]))), 6),
                    "position_notional_usdt": round(float(getattr(refreshed_account, "position_notional_usdt", report["account"].get("position_notional_usdt", 0.0))), 4),
                    "unrealized_pnl_usdt": round(float(getattr(refreshed_account, "unrealized_pnl_usdt", report["account"].get("unrealized_pnl_usdt", 0.0))), 4),
                    "cum_realized_pnl_usdt": round(float(getattr(refreshed_account, "cum_realized_pnl_usdt", report["account"].get("cum_realized_pnl_usdt", 0.0))), 4),
                    "total_equity_usdt": round(float(getattr(refreshed_account, "total_equity_usdt", report["account"].get("total_equity_usdt", 0.0))), 4),
                    "available_balance_usdt": round(float(getattr(refreshed_account, "available_balance_usdt", report["account"].get("available_balance_usdt", 0.0))), 4),
                    "leverage": round(float(getattr(refreshed_account, "leverage", report["account"].get("leverage", 0.0))), 4),
                    "liq_price": round(float(getattr(refreshed_account, "liq_price", report["account"].get("liq_price", 0.0))), 6),
                    "position_im_usdt": round(float(getattr(refreshed_account, "position_im_usdt", report["account"].get("position_im_usdt", 0.0))), 4),
                    "position_mm_usdt": round(float(getattr(refreshed_account, "position_mm_usdt", report["account"].get("position_mm_usdt", 0.0))), 4),
                    "take_profit_price": round(float(getattr(refreshed_account, "take_profit_price", report["account"].get("take_profit_price", 0.0))), 6),
                    "stop_loss_price": round(float(getattr(refreshed_account, "stop_loss_price", report["account"].get("stop_loss_price", 0.0))), 6),
                    "trailing_stop_distance": round(float(getattr(refreshed_account, "trailing_stop_distance", report["account"].get("trailing_stop_distance", 0.0))), 6),
                    "position_status": str(getattr(refreshed_account, "position_status", report["account"].get("position_status", "Normal"))),
                    "is_reduce_only": bool(getattr(refreshed_account, "is_reduce_only", report["account"].get("is_reduce_only", False))),
                    "liquidation_buffer_pct": round(
                        _perp_liquidation_buffer_pct(
                            float(getattr(refreshed_account, "mark_price", report["account"].get("mark_price", report["last_price"])) or report["last_price"]),
                            float(getattr(refreshed_account, "liq_price", report["account"].get("liq_price", 0.0)) or 0.0),
                        ),
                        4,
                    ),
                }
            )
        except Exception as exc:
            report["protection_result"] = {"status": "error", "reason": str(exc)}
    if str(result.get("status", "")).lower() in {"accepted", "filled"} and not bool(order.get("reduce_only")):
        entry_count_after_fill = _record_position_policy_entry_fill(
            position_policy_state,
            mode=mode,
            symbol=report["selected_symbol"],
            position_side=str(report["account"].get("position_side", "flat")).strip().lower(),
            entry_price=float(report["account"].get("entry_price", report["last_price"]) or report["last_price"]),
            net_position=float(report["account"].get("net_position", 0.0) or 0.0),
            now_epoch=time.time(),
        )
        current_state = position_policy_state.get(_position_policy_key(mode, report["selected_symbol"]), {})
        report["account"]["entry_count"] = entry_count_after_fill
        report["account"]["opened_at_epoch"] = round(float(current_state.get("opened_at_epoch", 0.0) or 0.0), 3)
        report["account"]["opened_at_local"] = (
            datetime.fromtimestamp(float(current_state.get("opened_at_epoch", 0.0)), tz=timezone.utc).astimezone(LOCAL_TZ).isoformat()
            if float(current_state.get("opened_at_epoch", 0.0) or 0.0) > 0
            else ""
        )
        report["position_context"] = {
            "is_open": True,
            "position_side": str(report["account"].get("position_side", "flat")),
            "hold_minutes": 0.0,
            "hold_bars": 0.0,
            "opened_at_epoch": float(current_state.get("opened_at_epoch", 0.0) or 0.0),
            "opened_at_local": str(report["account"].get("opened_at_local", "")),
            "entry_count": entry_count_after_fill,
        }
    if str(result.get("status", "")).lower() in {"accepted", "filled"}:
        _mark_trade_cooldown(
            storage.trade_cooldown_state,
            mode,
            report["selected_symbol"],
            _adaptive_trade_cooldown_seconds(report, settings, strategy_memory),
        )
    _save_position_policy_state(storage.position_policy_state, position_policy_state)
    trade_log_path = write_json_log(storage.trade_logs, "trade", report)
    evaluation_log_path = write_json_log(
        storage.evaluation_logs,
        "evaluation",
        {
            "symbol": report["selected_symbol"],
            "mode": mode,
            "evaluation": evaluation.__dict__,
            "idea": report["idea"],
        },
    )
    report["trade_log"] = str(trade_log_path)
    report["evaluation_log"] = str(evaluation_log_path)
    return _finalize_reporting(
        report=report,
        storage=storage,
        mode=mode,
        progress=progress,
        settings=settings,
        report_label="trade saved",
        daily_reviewer=daily_reviewer,
        strategy_reflector=strategy_reflector,
    )


def run(mode: str, symbol: str | None = None) -> int:
    report = execute_cycle(mode=mode, symbol=symbol)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local multi-agent crypto trading MVP.")
    parser.add_argument(
        "--mode",
        choices=["mock", "binance-testnet", "bybit-demo", "bybit-demo-perp"],
        default=None,
        help="Trading environment to use.",
    )
    parser.add_argument("--symbol", default=None, help="Trading pair such as BTC/USDT.")
    args = parser.parse_args()

    settings = load_settings()
    mode = args.mode or settings.trading_mode
    return run(mode=mode, symbol=args.symbol)


if __name__ == "__main__":
    raise SystemExit(main())
