from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

from trading_agents.agents import (
    DailyReviewAgent,
    ExecutorAgent,
    MarketCollectorAgent,
    PostTradeEvaluatorAgent,
    RiskSupervisorAgent,
    SelectorAgent,
    SentimentCollectorAgent,
    StrategistAgent,
    StrategyReflectionAgent,
)
from trading_agents.backtest import BacktestAgent
from trading_agents.config import load_settings
from trading_agents.exchange import (
    BinanceTestnetExchangeClient,
    BybitDemoExchangeClient,
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
    build_human_report,
    build_daily_summary,
    load_daily_summary_data,
    local_date_label,
    write_human_report,
    write_json_log,
    write_daily_summary,
)
from trading_agents.research import StrategyResearchAgent
from trading_agents.sentiment import SentimentDataProvider, write_sentiment_record
from trading_agents.storage import build_storage_layout
from trading_agents.strategy_memory import current_strategy_slot, load_strategy_memory, save_strategy_memory


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


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


def _cooldown_remaining_seconds(cooldowns: dict[str, float], symbol: str, now_epoch: float) -> float:
    return max(0.0, float(cooldowns.get(symbol, 0.0)) - now_epoch)


def _mark_trade_cooldown(path: Path, symbol: str, cooldown_seconds: float) -> None:
    if cooldown_seconds <= 0:
        return
    cooldowns = _load_trade_cooldowns(path)
    cooldowns[str(symbol)] = datetime.now(timezone.utc).timestamp() + cooldown_seconds
    _save_trade_cooldowns(path, cooldowns)


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
    human_content = build_human_report(report, mode=mode, symbol=report["selected_symbol"])
    human_report_path = write_human_report(
        storage.reports,
        report["selected_symbol"],
        mode,
        human_content,
    )
    report["human_report"] = str(human_report_path)
    date_label = local_date_label()
    daily_content = build_daily_summary(storage.trade_logs, date_label, storage.runner_log)
    daily_report_path = write_daily_summary(storage.daily_reports, date_label, daily_content)
    report["daily_report"] = str(daily_report_path)

    notion_sync = {"status": "disabled", "reason": "missing Notion token or status page id"}
    if settings.notion_api_token and settings.notion_status_page_id:
        try:
            if report.get("cycle_mode") != "full" and "result" not in report:
                notion_sync = {
                    "status": "skipped",
                    "reason": "fast-cycle status sync deferred to heartbeat",
                    "mode": "heartbeat_deferred",
                }
            else:
                daily_summary = load_daily_summary_data(storage.trade_logs, date_label, storage.runner_log)
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

    daily_review_sync = {"status": "disabled", "reason": "outside noon window or missing Notion daily review parent page id"}
    if (
        settings.notion_api_token
        and settings.notion_daily_review_parent_page_id
        and _load_runner_heartbeat(storage).get("timestamp")
        and datetime.now().astimezone().hour >= int(settings.notion_daily_review_hour)
    ):
        try:
            if _daily_review_already_published(storage.notion_daily_review_state, date_label):
                state = _read_json_file(storage.notion_daily_review_state)
                daily_review_sync = {
                    "status": "skipped",
                    "reason": "daily review already published for this Taiwan date",
                    "page_id": state.get("page_id", ""),
                    "mode": "daily_review",
                }
            else:
                daily_summary = load_daily_summary_data(storage.trade_logs, date_label, storage.runner_log)
                daily_review = daily_reviewer.evaluate(date_label, daily_summary)
                daily_review_sync = sync_notion_daily_review(
                    token=settings.notion_api_token,
                    parent_page_id=settings.notion_daily_review_parent_page_id,
                    date_label=date_label,
                    page_title_prefix=settings.notion_daily_review_title_prefix,
                    daily_review=daily_review.__dict__,
                    daily_summary=daily_summary,
                    state_path=storage.notion_daily_review_state,
                    lock_path=storage.notion_sync_lock,
                )
        except Exception as exc:
            daily_review_sync = {"status": "error", "reason": str(exc)}
    report["daily_review_sync"] = daily_review_sync

    strategy_memory_sync = {"status": "skipped", "reason": "12h reflection already up to date"}
    try:
        daily_summary = load_daily_summary_data(storage.trade_logs, date_label, storage.runner_log)
        current_slot = current_strategy_slot()
        strategy_memory = load_strategy_memory(storage.strategy_memory_state)
        if strategy_memory.get("slot") != current_slot:
            reflection = strategy_reflector.evaluate(current_slot, daily_summary)
            payload = {
                "slot": reflection.slot,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "summary": reflection.summary,
                "biases": reflection.biases,
                "risk_adjustments": reflection.risk_adjustments,
                "focus_symbols": reflection.focus_symbols,
            }
            save_strategy_memory(storage.strategy_memory_state, payload)
            strategy_memory_sync = {"status": "updated", "slot": current_slot}
    except Exception as exc:
        strategy_memory_sync = {"status": "error", "reason": str(exc)}
    report["strategy_memory_sync"] = strategy_memory_sync
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
        )
    if mode == "bybit-demo":
        return BybitDemoExchangeClient(
            api_key=settings.bybit_demo_api_key,
            secret=settings.bybit_demo_secret,
        )
    return MockExchangeClient(initial_balance_usdt=settings.initial_balance_usdt)


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
        )

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    now_epoch = datetime.now(timezone.utc).timestamp()
    cooldowns = _load_trade_cooldowns(storage.trade_cooldown_state)
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
    sentiment_provider = SentimentDataProvider(
        config_path=settings.sentiment_config_path,
        timeout_seconds=settings.sentiment_request_timeout_seconds,
        cache_ttl_seconds=settings.sentiment_cache_ttl_seconds,
        cache_state_path=storage.sentiment_http_cache_state,
    )
    sentiment_collector = SentimentCollectorAgent(provider=sentiment_provider)
    backtester = BacktestAgent()
    strategy_researcher = StrategyResearchAgent(settings.strategy_library_path, llm_client=analysis_llm_client)
    strategist = StrategistAgent(llm_client=analysis_llm_client)
    supervisor = RiskSupervisorAgent(llm_client=analysis_llm_client)
    selector = SelectorAgent(llm_client=analysis_llm_client)
    executor = ExecutorAgent()
    evaluator = PostTradeEvaluatorAgent(llm_client=analysis_llm_client)
    daily_reviewer = DailyReviewAgent(llm_client=llm_client)
    strategy_reflector = StrategyReflectionAgent(llm_client=llm_client)
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
        available_usdt = account.free_usdt
        actual_base_asset = account.base_asset
        min_order_value_usdt = 0.0
        if hasattr(exchange, "minimum_order_value_usdt"):
            try:
                min_order_value_usdt = float(exchange.minimum_order_value_usdt(candidate_symbol))
            except Exception:
                min_order_value_usdt = 0.0
        available_base_asset, dust_info = _normalize_dust_position(
            base_asset=actual_base_asset,
            last_price=float(snapshot.last_price),
            min_order_value_usdt=min_order_value_usdt,
            dust_position_multiplier=settings.dust_position_multiplier,
        )

        market_summary = market_collector.summarize(snapshot)
        if dust_info.get("is_dust"):
            market_summary = (
                f"{market_summary}; dust position ignored for execution "
                f"({float(dust_info.get('dust_notional_usdt', 0.0)):.2f} < "
                f"{float(dust_info.get('dust_threshold_usdt', 0.0)):.2f} USDT)"
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
            lambda: strategy_researcher.evaluate_with_memory(snapshot, sentiment, strategy_memory),
        )
        progress("strategy_researcher", "done", strategy_research.summary)
        progress("strategist", "running", f"generating trade idea for {candidate_symbol}")
        idea = timed_stage(
            "strategist",
            lambda: strategist.evaluate(
                snapshot,
                sentiment,
                backtest,
                strategy_research,
                available_usdt=available_usdt,
                available_base_asset=available_base_asset,
                min_order_value_usdt=min_order_value_usdt,
                aggressive_mode=settings.demo_aggressive_mode,
                trading_mode=mode,
                strategy_memory=strategy_memory,
            ),
        )
        risk_feedback = ""
        progress("strategist", "done", f"{candidate_symbol}: {idea.action} ({idea.score:.2f})")
        progress("risk_supervisor", "running", f"reviewing {candidate_symbol}")
        use_candidate_llm_risk = bool(analysis_llm_client) and cycle_mode == "full" and not settings.llm_selected_candidate_only
        if use_candidate_llm_risk:
            risk_feedback = timed_stage(
                "risk_supervisor",
                lambda: supervisor.critique(
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
                    lambda: strategist.refine_with_risk_feedback(
                        idea,
                        risk_feedback,
                        available_usdt=available_usdt,
                        available_base_asset=available_base_asset,
                        strategy_memory=strategy_memory,
                    ),
                )
                progress("strategist", "done", f"{candidate_symbol}: revised to {idea.action} ({idea.score:.2f})")
        approval = timed_stage(
            "risk_supervisor",
            lambda: supervisor.review(
                idea=idea,
                sentiment=sentiment,
                backtest=backtest,
                strategy_research=strategy_research,
                available_usdt=available_usdt,
                available_base_asset=available_base_asset,
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
            ),
        )
        cooldown_remaining = _cooldown_remaining_seconds(cooldowns, candidate_symbol, now_epoch)
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
                    "dust_position": bool(dust_info.get("is_dust")),
                    "dust_notional_usdt": round(float(dust_info.get("dust_notional_usdt", 0.0)), 4),
                },
                "execution_constraints": {
                    "min_order_value_usdt": round(min_order_value_usdt, 4),
                    "cooldown_remaining_seconds": int(cooldown_remaining),
                    "dust_threshold_usdt": round(float(dust_info.get("dust_threshold_usdt", 0.0)), 4),
                },
                "debate": {
                    "risk_feedback": risk_feedback,
                },
            }
        )

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
        and selected.get("idea", {}).get("action") != "hold"
        and selected.get("approval", {}).get("approved")
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
            ),
        )
        selected["approval"] = selected_approval.__dict__
        selected["debate"] = {"risk_feedback": risk_feedback}
        progress("risk_supervisor", "done", f"{selected['symbol']}: {selected_approval.reason}")

    report = {
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
        "execution_constraints": selected.get("execution_constraints", {}),
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
        buy_balance_buffer_pct=settings.buy_balance_buffer_pct,
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
    if str(result.get("status", "")).lower() in {"accepted", "filled"}:
        _mark_trade_cooldown(
            storage.trade_cooldown_state,
            report["selected_symbol"],
            settings.trade_cooldown_seconds,
        )
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
        choices=["mock", "binance-testnet", "bybit-demo"],
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
