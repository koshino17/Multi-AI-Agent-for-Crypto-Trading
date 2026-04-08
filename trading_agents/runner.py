from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from trading_agents.storage import build_storage_layout


_running = True

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
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _write_pid(path: Path) -> None:
    path.write_text(str(os.getpid()))


def _remove_pid(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _timeframe_seconds(label: str) -> int:
    return _TIMEFRAME_SECONDS.get(label, 900)


def _bucket_id(now: datetime, timeframe: str) -> int:
    seconds = _timeframe_seconds(timeframe)
    return int(now.timestamp()) // seconds


def _account_tuple(snapshot: dict) -> tuple[float, float]:
    account = snapshot.get("account", {})
    return (
        round(float(account.get("free_usdt", 0.0)), 6),
        round(float(account.get("base_asset", 0.0)), 8),
    )


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
        market = exchange.fetch_snapshot(candidate_symbol, timeframe)
        account = exchange.fetch_account_state(candidate_symbol)
        prices[candidate_symbol] = float(market.last_price)
        accounts[candidate_symbol] = (round(account.free_usdt, 6), round(account.base_asset, 8))
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
        if not baseline or held_state[1] <= 0:
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
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    _write_pid(storage.runner_pid)
    exchange = _build_exchange(mode, settings)
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
    }

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
                    _emit(report)
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
        _remove_pid(storage.runner_pid)


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run the trading agents continuously.")
    parser.add_argument("--mode", choices=["mock", "binance-testnet", "bybit-demo"], default=settings.trading_mode)
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
