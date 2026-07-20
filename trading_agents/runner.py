from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import time
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

from trading_agents.config import load_settings
from trading_agents.main import _build_exchange, _parse_symbol_pool, execute_cycle
from trading_agents.notion_sync import sync_notion_heartbeat
from trading_agents.reporting import load_daily_summary_data, local_date_label
from trading_agents.strategy_research import run_strategy_research_cycle
from trading_agents.storage import build_storage_layout, mode_storage_root


_running = True
_RUNNER_LOG_PATH: Path | None = None
_RUNNER_LOCK_FD: int | None = None
_MAX_RUNNER_LOG_BYTES = 64 * 1024 * 1024

_TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _stop(_: int, __) -> None:
    global _running
    _running = False


def _emit(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    print(line, flush=True)
    if _RUNNER_LOG_PATH is not None:
        try:
            _RUNNER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _RUNNER_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def _write_pid(path: Path) -> None:
    path.write_text(str(os.getpid()))


def _remove_pid(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _rotate_large_log(path: Path, max_bytes: int = _MAX_RUNNER_LOG_BYTES) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        rotated = path.with_name(f"{path.name}.1")
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except OSError:
        pass


def _write_runner_status(path: Path, payload: dict) -> None:
    body = dict(payload)
    body.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _credential_blocker(mode: str, settings) -> str | None:
    if mode in {"bybit-demo", "bybit-demo-perp"}:
        if not settings.bybit_demo_api_key or not settings.bybit_demo_secret:
            return "Missing Bybit Demo API credentials."
    if mode == "binance-testnet":
        if not settings.binance_testnet_api_key or not settings.binance_testnet_secret:
            return "Missing Binance Testnet API credentials."
    return None


def _acquire_runner_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def _release_runner_lock(fd: int | None, path: Path) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    try:
        path.unlink()
    except OSError:
        pass


def _cycle_report_summary(report: dict) -> dict:
    idea = report.get("idea") if isinstance(report.get("idea"), dict) else {}
    approval = report.get("approval") if isinstance(report.get("approval"), dict) else {}
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    execution_profile = {}
    if candidates and isinstance(candidates[0], dict):
        strategy_research = candidates[0].get("strategy_research")
        if isinstance(strategy_research, dict):
            execution_profile = strategy_research.get("selected_execution_profile") or {}
    return {
        "event": "cycle_report",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": report.get("mode"),
        "symbol": report.get("selected_symbol"),
        "cycle_mode": report.get("cycle_mode"),
        "cycle_reason": report.get("cycle_reason"),
        "action": idea.get("action"),
        "score": idea.get("score"),
        "approved": approval.get("approved"),
        "decision_source": report.get("decision_source"),
        "result_status": result.get("status"),
        "trade_log": report.get("trade_log"),
        "daily_report": report.get("daily_report"),
        "human_report": report.get("human_report"),
        "entry_ttl_seconds": execution_profile.get("entry_ttl_seconds"),
        "notion_sync_status": (report.get("notion_sync") or {}).get("status") if isinstance(report.get("notion_sync"), dict) else None,
    }


def _timeframe_seconds(label: str) -> int:
    return _TIMEFRAME_SECONDS.get(label, 900)


def _bucket_id(now: datetime, timeframe: str) -> int:
    seconds = _timeframe_seconds(timeframe)
    return int(now.timestamp()) // seconds


def _account_tuple(snapshot: dict) -> tuple[float, float]:
    account = snapshot.get("account", {})
    return (
        round(float(account.get("free_usdt", 0.0)), 6),
        round(float(account.get("net_position", account.get("base_asset", 0.0))), 8),
    )


def _all_positions_flat(snapshot: dict, epsilon: float = 1e-9) -> bool:
    return all(abs(float(position)) <= epsilon for _, position in snapshot.get("accounts", {}).values())


def _parse_research_limits(raw_limits: tuple[str, ...]) -> tuple[int, ...]:
    limits: list[int] = []
    for item in raw_limits:
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if value >= 120:
            limits.append(min(value, 1000))
    return tuple(dict.fromkeys(limits)) or (320, 1000)


def _capture_cycle_state(report: dict, timeframe: str) -> dict:
    candidates = report.get("candidates", [])
    selected_strategy_backtest = report.get("selected_strategy_backtest") or report.get("backtest", {})
    return {
        "cycle_at": time.time(),
        "cycle_bucket": _bucket_id(datetime.now(timezone.utc), timeframe),
        "prices": {item["symbol"]: float(item["last_price"]) for item in candidates},
        "accounts": {item["symbol"]: _account_tuple(item) for item in candidates},
        "selected_symbol": report.get("selected_symbol"),
        "selected_action": report.get("idea", {}).get("action"),
        "selected_score": float(report.get("idea", {}).get("score", 0.0)),
        "selected_expectancy_pct": float(selected_strategy_backtest.get("expectancy_pct", 0.0)),
        "selected_profit_factor": float(selected_strategy_backtest.get("profit_factor", 0.0)),
    }


def _monitor_snapshot(exchange, symbol_pool: list[str], timeframe: str) -> dict:
    prices: dict[str, float] = {}
    accounts: dict[str, tuple[float, float]] = {}
    for candidate_symbol in symbol_pool:
        market = exchange.fetch_snapshot(candidate_symbol, timeframe, include_microstructure=False)
        account = exchange.fetch_account_state(candidate_symbol)
        prices[candidate_symbol] = float(market.last_price)
        accounts[candidate_symbol] = (
            round(float(getattr(account, "free_usdt", 0.0)), 6),
            round(float(getattr(account, "net_position", getattr(account, "base_asset", 0.0))), 8),
        )
    return {"prices": prices, "accounts": accounts}


def _should_run_cycle(
    monitor_state: dict,
    snapshot: dict,
    timeframe: str,
    price_trigger_pct: float,
    micro_cycle_trigger_pct: float,
    position_micro_trigger_pct: float,
    max_gap_seconds: float,
) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    current_bucket = _bucket_id(now, timeframe)
    last_cycle_at = monitor_state.get("cycle_at")
    last_cycle_bucket = monitor_state.get("cycle_bucket")
    cycle_prices = monitor_state.get("prices", {})
    last_seen_accounts = monitor_state.get("last_seen_accounts")

    if last_cycle_at is None:
        return True, "initial boot trigger"
    if current_bucket != last_cycle_bucket:
        return True, f"new {timeframe} candle window"
    if time.time() - float(last_cycle_at) >= max_gap_seconds:
        return True, f"safety refresh after {int(max_gap_seconds)}s"

    current_accounts = snapshot["accounts"]
    if last_seen_accounts is not None and current_accounts != last_seen_accounts:
        changed_symbols = [
            symbol
            for symbol, state in current_accounts.items()
            if last_seen_accounts.get(symbol) != state
        ]
        return True, f"account state changed: {', '.join(changed_symbols)}"

    for candidate_symbol, price in snapshot["prices"].items():
        baseline = cycle_prices.get(candidate_symbol)
        held_state = current_accounts.get(candidate_symbol, (0.0, 0.0))
        if not baseline or abs(held_state[1]) <= 0:
            continue
        move_pct = abs(price - baseline) / baseline if baseline else 0.0
        if move_pct >= position_micro_trigger_pct:
            return True, f"{candidate_symbol} held-position micro trigger ({move_pct:.2%})"

    selected_symbol = monitor_state.get("selected_symbol")
    selected_action = str(monitor_state.get("selected_action", ""))
    selected_score = float(monitor_state.get("selected_score", 0.0))
    selected_expectancy_pct = float(monitor_state.get("selected_expectancy_pct", 0.0))
    selected_profit_factor = float(monitor_state.get("selected_profit_factor", 0.0))
    if selected_symbol and selected_symbol in snapshot["prices"]:
        baseline = cycle_prices.get(selected_symbol)
        if baseline:
            move_pct = abs(snapshot["prices"][selected_symbol] - baseline) / baseline
            if selected_action in {"buy", "sell"} and selected_score >= 0.52 and move_pct >= micro_cycle_trigger_pct:
                return True, f"{selected_symbol} micro timing trigger ({move_pct:.2%})"
            if (
                selected_action == "hold"
                and selected_expectancy_pct >= 0.05
                and selected_profit_factor >= 1.10
                and move_pct >= micro_cycle_trigger_pct
            ):
                return True, f"{selected_symbol} setup activation trigger ({move_pct:.2%})"

    for candidate_symbol, price in snapshot["prices"].items():
        baseline = cycle_prices.get(candidate_symbol)
        if not baseline:
            continue
        move_pct = abs(price - baseline) / baseline if baseline else 0.0
        if move_pct >= price_trigger_pct:
            return True, f"{candidate_symbol} moved {move_pct:.2%} since last decision"

    return False, "monitoring for new candle, account change, or breakout"


def _classify_cycle_mode(reason: str) -> str:
    lowered = reason.lower()
    if any(
        marker in lowered
        for marker in (
            "initial boot trigger",
            "new ",
            "safety refresh",
            "account state changed",
        )
    ):
        return "full"
    return "fast"


def loop(mode: str, symbol: str | None, interval_seconds: float) -> int:
    global _RUNNER_LOCK_FD, _RUNNER_LOG_PATH
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    settings = load_settings()
    storage = build_storage_layout(str(mode_storage_root(settings.data_root, mode)))
    _rotate_large_log(storage.runner_log)
    _RUNNER_LOG_PATH = storage.runner_log
    _RUNNER_LOCK_FD = _acquire_runner_lock(storage.runner_lock)
    if _RUNNER_LOCK_FD is None:
        _write_runner_status(
            storage.runner_status,
            {
                "status": "duplicate_blocked",
                "mode": mode,
                "symbol": symbol,
                "detail": f"another runner already holds {storage.runner_lock}",
            },
        )
        _emit(
            {
                "event": "runner",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "duplicate_blocked",
                "mode": mode,
                "symbol": symbol,
                "detail": f"another runner already holds {storage.runner_lock}",
            }
        )
        while _running:
            time.sleep(30)
        return 0
    _write_pid(storage.runner_pid)
    symbol_pool = _parse_symbol_pool(symbol, settings)
    timeframe = settings.timeframe
    monitor_interval = max(interval_seconds, 5.0)
    max_gap_seconds = max(settings.run_interval_seconds, monitor_interval)
    price_trigger_pct = max(settings.price_trigger_pct, 0.001)
    micro_cycle_trigger_pct = max(settings.micro_cycle_trigger_pct, 0.001)
    position_micro_trigger_pct = max(settings.position_micro_trigger_pct, 0.001)
    monitor_state: dict = {
        "cycle_at": None,
        "cycle_bucket": None,
        "prices": {},
        "accounts": {},
        "last_seen_accounts": None,
        "last_notion_heartbeat_sync_at": 0.0,
        "selected_symbol": None,
        "selected_action": "hold",
        "selected_score": 0.0,
        "selected_expectancy_pct": 0.0,
        "selected_profit_factor": 0.0,
        "last_strategy_research_at": 0.0,
    }
    strategy_research_every = max(float(settings.strategy_research_refresh_hours or 0.0), 0.0) * 3600.0
    strategy_research_limits = _parse_research_limits(settings.strategy_research_focus_limits)

    blocker = _credential_blocker(mode, settings)
    if blocker:
        _write_runner_status(
            storage.runner_status,
            {
                "status": "blocked",
                "mode": mode,
                "symbol": ",".join(symbol_pool) or symbol,
                "detail": blocker,
                "reason_code": "missing_exchange_credentials",
            },
        )
        _emit(
            {
                "event": "runner",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "blocked",
                "mode": mode,
                "symbol_pool": symbol_pool,
                "timeframe": timeframe,
                "monitor_interval_seconds": monitor_interval,
                "detail": blocker,
            }
        )
        while _running:
            time.sleep(30)
        return 0

    exchange = _build_exchange(mode, settings)

    _emit(
        {
            "event": "runner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "started",
            "mode": mode,
            "symbol_pool": symbol_pool,
            "timeframe": timeframe,
            "monitor_interval_seconds": monitor_interval,
            "max_decision_gap_seconds": max_gap_seconds,
            "price_trigger_pct": price_trigger_pct,
            "micro_cycle_trigger_pct": micro_cycle_trigger_pct,
            "position_micro_trigger_pct": position_micro_trigger_pct,
        }
    )
    _write_runner_status(
        storage.runner_status,
        {
            "status": "started",
            "mode": mode,
            "symbol": ",".join(symbol_pool) or symbol,
            "detail": "runner active",
            "timeframe": timeframe,
        },
    )

    try:
        while _running:
            try:
                snapshot = _monitor_snapshot(exchange, symbol_pool, timeframe)
            except Exception as exc:
                _emit(
                    {
                        "event": "monitor",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "error",
                        "detail": f"monitor snapshot failed: {exc}",
                    }
                )
                deadline = time.time() + monitor_interval
                while _running and time.time() < deadline:
                    time.sleep(1)
                continue
            should_run, reason = _should_run_cycle(
                monitor_state,
                snapshot,
                timeframe,
                price_trigger_pct,
                micro_cycle_trigger_pct,
                position_micro_trigger_pct,
                max_gap_seconds,
            )
            monitor_state["last_seen_accounts"] = snapshot["accounts"]
            _emit(
                {
                    "event": "monitor",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "triggered" if should_run else "watching",
                    "detail": reason,
                    "prices": snapshot["prices"],
                }
            )

            if settings.notion_api_token and settings.notion_status_page_id:
                now_epoch = time.time()
                heartbeat_every = max(settings.notion_heartbeat_sync_seconds, monitor_interval)
                if now_epoch - float(monitor_state.get("last_notion_heartbeat_sync_at", 0.0)) >= heartbeat_every:
                    heartbeat_timestamp = datetime.now(timezone.utc).isoformat()
                    try:
                        daily_summary = load_daily_summary_data(
                            storage.trade_logs,
                            local_date_label(),
                            storage.runner_log,
                        )
                        sync_result = sync_notion_heartbeat(
                            token=settings.notion_api_token,
                            page_id=settings.notion_status_page_id,
                            page_title=settings.notion_status_page_title,
                            daily_summary=daily_summary,
                            runner_heartbeat={
                                "text": f"{heartbeat_timestamp} ({reason})",
                                "timestamp": heartbeat_timestamp,
                            },
                            lock_path=storage.notion_sync_lock,
                        )
                        monitor_state["last_notion_heartbeat_sync_at"] = now_epoch
                        _emit(
                            {
                                "event": "notion",
                                "timestamp": heartbeat_timestamp,
                                "status": sync_result.get("status", "unknown"),
                                "mode": sync_result.get("mode", "heartbeat"),
                            }
                        )
                    except Exception as exc:
                        _emit(
                            {
                                "event": "notion",
                                "timestamp": heartbeat_timestamp,
                                "status": "error",
                                "mode": "heartbeat",
                                "detail": str(exc),
                            }
                        )

            if settings.strategy_research_enabled and strategy_research_every > 0:
                now_epoch = time.time()
                research_due = now_epoch - float(monitor_state.get("last_strategy_research_at", 0.0)) >= strategy_research_every
                can_research = (not settings.strategy_research_only_when_flat) or _all_positions_flat(snapshot)
                if research_due and can_research:
                    focus_symbol = symbol_pool[0] if symbol_pool else settings.symbol
                    try:
                        research_result = run_strategy_research_cycle(
                            settings=settings,
                            storage=storage,
                            focus_symbol=focus_symbol,
                            validation_symbols=settings.strategy_research_validation_symbols,
                            limits=strategy_research_limits,
                            include_alpha=settings.strategy_research_include_alpha,
                        )
                        monitor_state["last_strategy_research_at"] = now_epoch
                        _emit(
                            {
                                "event": "strategy_research",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "status": research_result.get("status", "updated"),
                                "focus_symbol": focus_symbol,
                                "recommendation": research_result.get("recommendation", {}),
                                "md_path": research_result.get("md_path", ""),
                            }
                        )
                    except Exception as exc:
                        _emit(
                            {
                                "event": "strategy_research",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "status": "error",
                                "detail": str(exc),
                                "focus_symbol": focus_symbol,
                            }
                        )

            if should_run:
                started_at = datetime.now(timezone.utc).isoformat()
                cycle_mode = _classify_cycle_mode(reason)

                def progress(stage: str, status: str, detail: str = "") -> None:
                    _emit(
                        {
                            "event": "stage",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "stage": stage,
                            "status": status,
                            "detail": detail,
                        }
                    )

                _emit(
                    {
                        "event": "cycle",
                        "timestamp": started_at,
                        "status": "started",
                        "mode": mode,
                        "symbol": symbol,
                        "reason": reason,
                        "cycle_mode": cycle_mode,
                    }
                )
                try:
                    report = execute_cycle(
                        mode=mode,
                        symbol=symbol,
                        progress_callback=progress,
                        cycle_mode=cycle_mode,
                        cycle_reason=reason,
                    )
                    monitor_state.update(_capture_cycle_state(report, timeframe))
                    _emit(
                        {
                            "event": "cycle",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "status": "finished",
                            "mode": mode,
                            "symbol": symbol,
                            "reason": reason,
                            "cycle_mode": cycle_mode,
                        }
                    )
                    _emit(_cycle_report_summary(report))
                except Exception as exc:
                    _emit(
                        {
                            "event": "cycle",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "status": "error",
                            "mode": mode,
                            "symbol": symbol,
                            "reason": reason,
                            "cycle_mode": cycle_mode,
                            "detail": str(exc),
                        }
                    )

            deadline = time.time() + monitor_interval
            while _running and time.time() < deadline:
                time.sleep(1)
        return 0
    finally:
        _write_runner_status(
            storage.runner_status,
            {
                "status": "stopped",
                "mode": mode,
                "symbol": ",".join(symbol_pool) or symbol,
                "detail": "runner stopped",
            },
        )
        _remove_pid(storage.runner_pid)
        _release_runner_lock(_RUNNER_LOCK_FD, storage.runner_lock)
        _RUNNER_LOCK_FD = None


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run the trading agents continuously.")
    parser.add_argument("--mode", choices=["mock", "binance-testnet", "bybit-demo", "bybit-demo-perp"], default=settings.trading_mode)
    parser.add_argument("--symbol", default=",".join(settings.observation_pool) or settings.symbol)
    parser.add_argument(
        "--interval",
        type=float,
        default=settings.monitor_interval_seconds,
        help="Monitor polling interval in seconds.",
    )
    args = parser.parse_args()
    return loop(mode=args.mode, symbol=args.symbol, interval_seconds=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
