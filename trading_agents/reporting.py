from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Taipei")
STAGE_DISPLAY_ORDER = (
    "market_collector",
    "sentiment_collector",
    "backtester",
    "strategy_researcher",
    "strategist",
    "risk_supervisor",
    "selector",
    "executor",
    "post_trade_evaluator",
)
STAGE_LABELS = {
    "market_collector": "Market",
    "sentiment_collector": "Sentiment",
    "backtester": "Backtest",
    "strategy_researcher": "Research",
    "strategist": "Strategist",
    "risk_supervisor": "Risk",
    "selector": "Selector",
    "executor": "Executor",
    "post_trade_evaluator": "Evaluator",
}


def _normalize_blocked_reason(reason: str) -> str:
    lowered = reason.lower()
    if lowered.startswith("position value below exchange minimum"):
        return "position value below exchange minimum"
    if lowered.startswith("max position below exchange minimum"):
        return "max position below exchange minimum"
    return reason


def _normalize_result_reason(reason: str) -> str:
    lowered = reason.lower()
    if "below bybit minimum" in lowered or "below exchange minimum" in lowered:
        return "exchange rejected below minimum"
    if "insufficient" in lowered:
        return "exchange rejected insufficient balance"
    return reason or "unknown exchange rejection"


def _result_status(item: dict[str, Any]) -> str:
    result = item.get("result")
    if not isinstance(result, dict):
        return ""
    status = str(result.get("status", "")).lower()
    if status in {"accepted", "filled"}:
        return "accepted"
    if status == "rejected":
        return "rejected"
    return status


def _result_reason(item: dict[str, Any]) -> str:
    result = item.get("result")
    if not isinstance(result, dict):
        return ""
    return _normalize_result_reason(str(result.get("exchange_error") or result.get("reason") or ""))


def _record_mode(item: dict[str, Any]) -> str:
    return str(item.get("mode", "")).strip().lower()


def _is_perp_record(item: dict[str, Any]) -> bool:
    return "perp" in _record_mode(item)


def _order_payload(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result")
    if isinstance(result, dict):
        order = result.get("order")
        if isinstance(order, dict):
            return order
    order = item.get("order")
    return order if isinstance(order, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_stage_latency_breakdown(stage_latency_seconds: dict[str, float], limit: int | None = None) -> str:
    ordered_items = [
        (stage, float(stage_latency_seconds.get(stage, 0.0)))
        for stage in STAGE_DISPLAY_ORDER
        if float(stage_latency_seconds.get(stage, 0.0)) > 0
    ]
    if limit is not None:
        ordered_items = ordered_items[:limit]
    if not ordered_items:
        return "n/a"
    return " | ".join(f"{STAGE_LABELS.get(stage, stage)}={value:.2f}s" for stage, value in ordered_items)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    q = min(max(float(quantile), 0.0), 1.0)
    index = q * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%S%fZ")


def _path_sort_key(path: Path) -> str:
    name = path.stem
    if "-" not in name:
        return name
    return name.rsplit("-", 1)[-1]


def _path_timestamp(path: Path) -> datetime | None:
    stamp = _path_sort_key(path)
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def write_json_log(path: Path, prefix: str, payload: dict) -> Path:
    target = path / f"{prefix}-{_stamp()}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return target


def build_human_report(report: dict, mode: str, symbol: str) -> str:
    sentiment = report["sentiment"]
    backtest = report.get("backtest")
    strategy_research = report.get("strategy_research")
    idea = report["idea"]
    approval = report["approval"]
    summary_line = _summary_line(report)
    lines = [
        f"# Latest Trading Summary: {symbol}",
        "",
        "## Quick View",
        "",
        f"- Conclusion: {summary_line}",
        f"- Time: {_local_now().isoformat()}",
        f"- Mode: {mode}",
        f"- Signal: {idea['action']} (score={idea['score']:.2f})",
        f"- Risk Decision: {approval['reason']}",
    ]
    if report.get("selection_summary"):
        lines.append(f"- Selection: {report['selection_summary']}")
    account = report.get("account")
    if account:
        if account.get("market_type") == "perp":
            account_line = (
                f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT | "
                f"position {account.get('position_side', 'flat')} "
                f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']} "
                f"@ {float(account.get('entry_price', 0.0)):.4f} | "
                f"UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT"
            )
        else:
            account_line = f"- Account: {account['free_usdt']:.2f} USDT + {account['base_asset']:.6f} {account['base_symbol']}"
            if account.get("dust_position"):
                account_line += f" (dust ignored for execution: {float(account.get('dust_notional_usdt', 0.0)):.2f} USDT)"
        lines.append(account_line)

    warnings = approval.get("warnings", [])
    if warnings:
        lines.append(f"- Main Risk: {'; '.join(warnings[:2])}")

    order = report.get("order")
    if order:
        lines.append(
            f"- Order: {order['side']} {order['symbol']} "
            f"qty={order['quantity']} notional={order['notional_usdt']} USDT"
        )

    lines.extend(
        [
            "",
            "## Why",
            "",
            f"- Market: {report['market_summary']}",
            f"- Sentiment: {sentiment['summary']}",
            f"- Replay Test: {backtest['summary']}" if backtest else "- Replay Test: unavailable",
            (
                f"- Strategy Research: {strategy_research['summary']}"
                if strategy_research
                else "- Strategy Research: unavailable"
            ),
            f"- Reason: {idea['rationale']}",
        ]
    )
    debate = report.get("debate") or {}
    if debate.get("risk_feedback"):
        lines.append(f"- Debate: risk raised `{debate['risk_feedback']}` before final decision")

    strategy_candidates = strategy_research.get("candidates", []) if strategy_research else []
    if strategy_candidates:
        lines.extend(["", "## Strategy Library", ""])
        ranked_strategies = sorted(
            strategy_candidates,
            key=lambda item: float(item["backtest"]["cumulative_return_pct"]),
            reverse=True,
        )
        for item in ranked_strategies:
            marker = " <- selected" if item["strategy_id"] == strategy_research.get("selected_strategy_id") else ""
            lines.append(
                f"- {item['strategy_id']}: {item['backtest']['summary']} ({item['source']}, {item['credibility']}){marker}"
            )

    candidates = report.get("candidates", [])
    if candidates:
        lines.extend(["", "## Candidate Ranking", ""])
        ranked = sorted(
            candidates,
            key=lambda item: (
                item["approval"]["approved"],
                float(item["idea"]["score"]),
                float(item["backtest"]["cumulative_return_pct"]),
            ),
            reverse=True,
        )
        for item in ranked:
            marker = " <- selected" if item["symbol"] == report.get("selected_symbol") else ""
            lines.append(
                f"- {item['symbol']}: {item['idea']['action']} ({float(item['idea']['score']):.2f}), "
                f"{item['approval']['reason']}, replay={item['backtest']['summary']}{marker}"
            )
            constraints = item.get("execution_constraints", {})
            if constraints.get("min_order_value_usdt"):
                lines.append(
                    f"- {item['symbol']} minimum order value: {float(constraints['min_order_value_usdt']):.2f} USDT"
                )

    result = report.get("result")
    if result:
        lines.append(f"- Execution: {result.get('status', 'unknown')}")

    evaluation = report.get("evaluation")
    if evaluation:
        lines.extend(
            [
                "",
                "## Result",
                "",
                f"- Evaluation: {evaluation['grade']}",
                f"- Note: {evaluation['notes']}",
            ]
        )

    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Sentiment log: {report['sentiment_log']}",
        ]
    )
    if "trade_log" in report:
        lines.append(f"- Trade log: {report['trade_log']}")
    if "evaluation_log" in report:
        lines.append(f"- Evaluation log: {report['evaluation_log']}")

    return "\n".join(lines) + "\n"


def _summary_line(report: dict) -> str:
    idea = report["idea"]
    approval = report["approval"]
    action = idea["action"]
    score = float(idea["score"])
    if not approval["approved"]:
        if action == "hold":
            return "目前沒有足夠訊號或可執行條件，建議先觀察。"
        return f"目前不建議下單，系統判定為 {action}，但風控未放行。"
    if action == "hold":
        return "目前沒有足夠訊號，建議先觀察。"
    return f"目前系統傾向 {action}，信心分數約 {score:.2f}，風控已放行。"


def write_human_report(path: Path, symbol: str, mode: str, content: str) -> Path:
    safe_symbol = symbol.replace("/", "-")
    safe_mode = mode.replace("/", "-")
    target = path / f"{safe_symbol}-{safe_mode}-summary-{_stamp()}.md"
    target.write_text(content)
    return target


def _load_daily_records(trade_logs_dir: Path, date_label: str) -> list[dict[str, Any]]:
    files = sorted(trade_logs_dir.glob("*.json"), key=_path_sort_key)
    today_files = []
    for path in files:
        timestamp = _path_timestamp(path)
        if timestamp is None:
            continue
        if timestamp.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") == date_label:
            today_files.append(path)
    records: list[dict[str, Any]] = []
    for path in today_files:
        try:
            records.append(json.loads(path.read_text()))
        except Exception:
            continue
    return records


def _load_all_records(trade_logs_dir: Path) -> list[dict[str, Any]]:
    files = sorted(trade_logs_dir.glob("*.json"), key=_path_sort_key)
    records: list[dict[str, Any]] = []
    for path in files:
        try:
            records.append(json.loads(path.read_text()))
        except Exception:
            continue
    return records


def _portfolio_from_record(record: dict[str, Any]) -> dict[str, Any]:
    candidates = record.get("candidates")
    positions: list[dict[str, Any]] = []
    free_usdt = 0.0
    total_equity_usdt = 0.0
    cumulative_realized_pnl_usdt = 0.0
    seen_symbols: set[str] = set()

    if isinstance(candidates, list) and candidates:
        for item in candidates:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in seen_symbols:
                continue
            account = item.get("account") or {}
            price = _safe_float(item.get("last_price"))
            market_type = str(account.get("market_type", "spot"))
            signed_quantity = _safe_float(account.get("net_position", account.get("base_asset")))
            quantity = abs(signed_quantity) if market_type == "perp" else _safe_float(account.get("base_asset"))
            if not free_usdt:
                free_usdt = _safe_float(account.get("available_balance_usdt", account.get("free_usdt")))
            total_equity_usdt = max(
                total_equity_usdt,
                _safe_float(account.get("total_equity_usdt")),
            )
            cumulative_realized_pnl_usdt += _safe_float(account.get("cum_realized_pnl_usdt"))
            positions.append(
                {
                    "symbol": symbol,
                    "asset": str(account.get("base_symbol") or symbol.split("/")[0]).strip(),
                    "quantity": quantity,
                    "signed_quantity": signed_quantity,
                    "price": price,
                    "value_usdt": (
                        abs(_safe_float(account.get("position_notional_usdt")))
                        if market_type == "perp"
                        else quantity * price
                    ),
                    "market_type": market_type,
                    "position_side": str(account.get("position_side", "flat")),
                    "entry_price": _safe_float(account.get("entry_price")),
                    "unrealized_pnl_usdt": _safe_float(account.get("unrealized_pnl_usdt")),
                }
            )
            seen_symbols.add(symbol)
    else:
        symbol = str(record.get("selected_symbol", "")).strip()
        account = record.get("account") or {}
        if symbol:
            price = _safe_float(record.get("last_price"))
            market_type = str(account.get("market_type", "spot"))
            signed_quantity = _safe_float(account.get("net_position", account.get("base_asset")))
            quantity = abs(signed_quantity) if market_type == "perp" else _safe_float(account.get("base_asset"))
            free_usdt = _safe_float(account.get("available_balance_usdt", account.get("free_usdt")))
            total_equity_usdt = _safe_float(account.get("total_equity_usdt"))
            cumulative_realized_pnl_usdt = _safe_float(account.get("cum_realized_pnl_usdt"))
            positions.append(
                {
                    "symbol": symbol,
                    "asset": str(account.get("base_symbol") or symbol.split("/")[0]).strip(),
                    "quantity": quantity,
                    "signed_quantity": signed_quantity,
                    "price": price,
                    "value_usdt": (
                        abs(_safe_float(account.get("position_notional_usdt")))
                        if market_type == "perp"
                        else quantity * price
                    ),
                    "market_type": market_type,
                    "position_side": str(account.get("position_side", "flat")),
                    "entry_price": _safe_float(account.get("entry_price")),
                    "unrealized_pnl_usdt": _safe_float(account.get("unrealized_pnl_usdt")),
                }
            )

    invested_value = sum(item["value_usdt"] for item in positions)
    total_value = total_equity_usdt if total_equity_usdt > 0 else free_usdt + invested_value
    return {
        "free_usdt": free_usdt,
        "invested_value_usdt": invested_value,
        "total_value_usdt": total_value,
        "cum_realized_pnl_usdt": cumulative_realized_pnl_usdt,
        "positions": positions,
    }


def _accepted_trade_rows(records: list[dict[str, Any]], taker_fee_pct: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        if _result_status(item) != "accepted":
            continue
        order = item.get("order")
        result = item.get("result")
        if not isinstance(order, dict) or not isinstance(result, dict):
            continue
        symbol = str(order.get("symbol") or item.get("selected_symbol") or "").strip()
        if not symbol:
            continue
        price = _safe_float(order.get("price")) or _safe_float(item.get("last_price"))
        quantity = _safe_float(result.get("submitted_qty"))
        if quantity <= 0:
            quantity = _safe_float(order.get("quantity"))
        notional = _safe_float(order.get("notional_usdt"))
        if notional <= 0 and quantity > 0 and price > 0:
            notional = quantity * price
        fee = _safe_float(result.get("fee"))
        effective_fee_pct = taker_fee_pct
        if str(item.get("mode", "")) == "bybit-demo-perp":
            effective_fee_pct = min(taker_fee_pct, 0.00055)
        if fee <= 0 and notional > 0:
            fee = notional * effective_fee_pct
        rows.append(
            {
                "symbol": symbol,
                "side": str(order.get("side", "")).lower(),
                "quantity": quantity,
                "price": price,
                "notional_usdt": notional,
                "fee_usdt": fee,
            }
        )
    return rows


def _build_financial_snapshot(
    records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    *,
    initial_balance_usdt: float,
    taker_fee_pct: float,
) -> dict[str, Any]:
    if not records:
        return {
            "initial_capital_usdt": initial_balance_usdt,
            "total_portfolio_value_usdt": 0.0,
            "cumulative_pnl_usdt": -initial_balance_usdt,
            "cumulative_pnl_pct": -100.0 if initial_balance_usdt > 0 else 0.0,
            "daily_pnl_usdt": 0.0,
            "daily_pnl_pct": 0.0,
            "realized_pnl_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "daily_fees_usdt": 0.0,
            "cumulative_fees_usdt": 0.0,
            "available_usdt": 0.0,
            "capital_utilization_pct": 0.0,
            "holdings": [],
        }

    start_snapshot = _portfolio_from_record(records[0])
    latest_snapshot = _portfolio_from_record(records[-1])
    accepted_today = _accepted_trade_rows(records, taker_fee_pct)
    accepted_all = _accepted_trade_rows(all_records, taker_fee_pct)
    is_perp = any(item.get("market_type") == "perp" for item in latest_snapshot.get("positions", []))

    if is_perp:
        latest_positions = latest_snapshot.get("positions", [])
        invested_value = sum(abs(_safe_float(item.get("value_usdt"))) for item in latest_positions)
        total_portfolio_value = _safe_float(latest_snapshot.get("total_value_usdt")) or initial_balance_usdt
        start_value = _safe_float(start_snapshot.get("total_value_usdt")) or initial_balance_usdt
        unrealized_pnl = sum(_safe_float(item.get("unrealized_pnl_usdt")) for item in latest_positions)
        state: dict[str, dict[str, float]] = {}
        for item in start_snapshot.get("positions", []):
            signed_qty = _safe_float(item.get("signed_quantity", item.get("quantity", 0.0)))
            if abs(signed_qty) <= 0:
                continue
            state[str(item.get("symbol", ""))] = {
                "signed_qty": signed_qty,
                "entry_price": _safe_float(item.get("entry_price", item.get("price", 0.0))),
            }
        realized_long_pnl = 0.0
        realized_short_pnl = 0.0
        for record in records:
            if _result_status(record) != "accepted" or not _is_perp_record(record):
                continue
            order = _order_payload(record)
            symbol = str(order.get("symbol", "")).strip()
            if not symbol:
                continue
            side = str(order.get("side", "")).lower()
            qty = _safe_float(order.get("quantity"))
            price = _safe_float(order.get("price"))
            notional = _safe_float(order.get("notional_usdt"))
            fee = _safe_float((record.get("result") or {}).get("fee"))
            if fee <= 0 and notional > 0:
                fee = notional * min(taker_fee_pct, 0.00055)
            if qty <= 0 or price <= 0 or side not in {"buy", "sell"}:
                continue
            current = state.setdefault(symbol, {"signed_qty": 0.0, "entry_price": price})
            signed_qty = _safe_float(current.get("signed_qty"))
            entry_price = _safe_float(current.get("entry_price"), price) or price
            if side == "buy":
                close_qty = min(qty, max(-signed_qty, 0.0))
                open_qty = max(qty - close_qty, 0.0)
                if close_qty > 0:
                    fee_share = fee * (close_qty / qty)
                    realized_short_pnl += (entry_price - price) * close_qty - fee_share
                    signed_qty += close_qty
                if open_qty > 0:
                    fee_share = fee * (open_qty / qty)
                    effective_open_price = price + (fee_share / open_qty if open_qty > 0 else 0.0)
                    if signed_qty > 0:
                        total_qty = signed_qty + open_qty
                        entry_price = ((signed_qty * entry_price) + (open_qty * effective_open_price)) / total_qty
                        signed_qty = total_qty
                    else:
                        signed_qty = open_qty
                        entry_price = effective_open_price
            else:
                close_qty = min(qty, max(signed_qty, 0.0))
                open_qty = max(qty - close_qty, 0.0)
                if close_qty > 0:
                    fee_share = fee * (close_qty / qty)
                    realized_long_pnl += (price - entry_price) * close_qty - fee_share
                    signed_qty -= close_qty
                if open_qty > 0:
                    fee_share = fee * (open_qty / qty)
                    effective_open_price = price - (fee_share / open_qty if open_qty > 0 else 0.0)
                    short_qty = max(-signed_qty, 0.0)
                    if short_qty > 0:
                        total_qty = short_qty + open_qty
                        entry_price = ((short_qty * entry_price) + (open_qty * effective_open_price)) / total_qty
                        signed_qty = -total_qty
                    else:
                        signed_qty = -open_qty
                        entry_price = effective_open_price
            if abs(signed_qty) <= 1e-12:
                signed_qty = 0.0
            current["signed_qty"] = signed_qty
            current["entry_price"] = entry_price if signed_qty != 0 else 0.0
        realized_pnl = realized_long_pnl + realized_short_pnl
        holdings: list[dict[str, Any]] = []
        current_long_exposure = 0.0
        current_short_exposure = 0.0
        for item in latest_positions:
            position_value = abs(_safe_float(item.get("value_usdt")))
            weight_pct = (position_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0
            entry_price = _safe_float(item.get("entry_price"))
            current_price = _safe_float(item.get("price"))
            side = str(item.get("position_side", "flat"))
            unrealized = _safe_float(item.get("unrealized_pnl_usdt"))
            if side == "long":
                current_long_exposure += position_value
            elif side == "short":
                current_short_exposure += position_value
            base_pct = 0.0
            if position_value > 0 and current_price > 0:
                base_pct = unrealized / position_value * 100
            holdings.append(
                {
                    "symbol": item.get("symbol"),
                    "asset": item.get("asset"),
                    "quantity": _safe_float(item.get("quantity")),
                    "signed_quantity": _safe_float(item.get("signed_quantity")),
                    "price": current_price,
                    "entry_price": entry_price,
                    "value_usdt": position_value,
                    "weight_pct": weight_pct,
                    "unrealized_pnl_usdt": unrealized,
                    "unrealized_pnl_pct": base_pct,
                    "position_side": side,
                    "market_type": "perp",
                }
            )
        holdings.sort(key=lambda item: float(item["value_usdt"]), reverse=True)
        daily_fees = sum(item["fee_usdt"] for item in accepted_today)
        daily_pnl = total_portfolio_value - start_value
        cumulative_pnl = total_portfolio_value - initial_balance_usdt
        return {
            "initial_capital_usdt": initial_balance_usdt,
            "total_portfolio_value_usdt": total_portfolio_value,
            "cumulative_pnl_usdt": cumulative_pnl,
            "cumulative_pnl_pct": (cumulative_pnl / initial_balance_usdt * 100) if initial_balance_usdt > 0 else 0.0,
            "daily_pnl_usdt": daily_pnl,
            "daily_pnl_pct": (daily_pnl / start_value * 100) if start_value > 0 else 0.0,
            "realized_pnl_usdt": realized_pnl,
            "realized_long_pnl_usdt": realized_long_pnl,
            "realized_short_pnl_usdt": realized_short_pnl,
            "unrealized_pnl_usdt": unrealized_pnl,
            "daily_fees_usdt": daily_fees,
            "cumulative_fees_usdt": sum(item["fee_usdt"] for item in accepted_all),
            "available_usdt": _safe_float(latest_snapshot.get("free_usdt")),
            "capital_utilization_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
            "current_long_exposure_usdt": current_long_exposure,
            "current_short_exposure_usdt": current_short_exposure,
            "holdings": holdings,
        }

    inventory: dict[str, dict[str, float]] = {}
    start_positions = {item["symbol"]: item for item in start_snapshot["positions"]}
    latest_positions = {item["symbol"]: item for item in latest_snapshot["positions"]}
    cash_usdt = _safe_float(start_snapshot.get("free_usdt"))

    for symbol, item in start_positions.items():
        inventory[symbol] = {
            "qty": _safe_float(item.get("quantity")),
            "cost_usdt": _safe_float(item.get("quantity")) * _safe_float(item.get("price")),
        }

    realized_pnl = 0.0
    daily_fees = 0.0
    for trade in accepted_today:
        symbol = trade["symbol"]
        quantity = max(trade["quantity"], 0.0)
        notional = max(trade["notional_usdt"], 0.0)
        fee = max(trade["fee_usdt"], 0.0)
        side = trade["side"]
        daily_fees += fee
        position = inventory.setdefault(symbol, {"qty": 0.0, "cost_usdt": 0.0})
        if quantity <= 0:
            continue
        if side == "buy":
            position["qty"] += quantity
            position["cost_usdt"] += notional + fee
            cash_usdt -= notional + fee
            continue
        proceeds = max(notional - fee, 0.0)
        cash_usdt += proceeds
        tracked_qty = max(position["qty"], 0.0)
        sell_qty = min(quantity, tracked_qty) if tracked_qty > 0 else 0.0
        if tracked_qty > 0 and sell_qty > 0:
            cost_portion = position["cost_usdt"] * (sell_qty / tracked_qty)
            position["qty"] = max(tracked_qty - sell_qty, 0.0)
            position["cost_usdt"] = max(position["cost_usdt"] - cost_portion, 0.0)
        else:
            cost_portion = notional
        realized_pnl += proceeds - cost_portion

    holdings: list[dict[str, Any]] = []
    unrealized_pnl = 0.0
    invested_value = 0.0

    symbols = set(latest_positions) | set(inventory)
    for symbol in symbols:
        latest_item = latest_positions.get(symbol) or start_positions.get(symbol) or {"asset": symbol.split("/")[0]}
        tracked = inventory.setdefault(symbol, {"qty": 0.0, "cost_usdt": 0.0})
        actual_qty = max(_safe_float(tracked.get("qty")), 0.0)
        current_price = _safe_float(latest_item.get("price"))
        current_value = actual_qty * current_price
        tracked_cost = max(_safe_float(tracked.get("cost_usdt")), 0.0)
        holding_unrealized = current_value - tracked_cost
        unrealized_pnl += holding_unrealized
        invested_value += current_value
        holdings.append(
            {
                "symbol": symbol,
                "asset": latest_item.get("asset", symbol.split("/")[0]),
                "quantity": actual_qty,
                "price": current_price,
                "value_usdt": current_value,
                "weight_pct": 0.0,
                "unrealized_pnl_usdt": holding_unrealized,
                "unrealized_pnl_pct": (holding_unrealized / tracked_cost * 100) if tracked_cost > 0 else 0.0,
            }
        )

    holdings.sort(key=lambda item: float(item["value_usdt"]), reverse=True)
    start_value = _safe_float(start_snapshot.get("total_value_usdt"))
    total_portfolio_value = cash_usdt + invested_value
    for item in holdings:
        item["weight_pct"] = (float(item["value_usdt"]) / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0
    daily_pnl = total_portfolio_value - start_value
    cumulative_pnl = total_portfolio_value - initial_balance_usdt

    return {
        "initial_capital_usdt": initial_balance_usdt,
        "total_portfolio_value_usdt": total_portfolio_value,
        "cumulative_pnl_usdt": cumulative_pnl,
        "cumulative_pnl_pct": (cumulative_pnl / initial_balance_usdt * 100) if initial_balance_usdt > 0 else 0.0,
        "daily_pnl_usdt": daily_pnl,
        "daily_pnl_pct": (daily_pnl / start_value * 100) if start_value > 0 else 0.0,
        "realized_pnl_usdt": realized_pnl,
        "unrealized_pnl_usdt": unrealized_pnl,
        "daily_fees_usdt": daily_fees,
        "cumulative_fees_usdt": sum(item["fee_usdt"] for item in accepted_all),
        "available_usdt": cash_usdt,
        "capital_utilization_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
        "holdings": holdings,
    }


def _load_runner_event_counts(runner_log_path: Path | None, date_label: str) -> dict[str, float]:
    if runner_log_path is None or not runner_log_path.exists():
        return {"monitor_heartbeats": 0, "avg_decision_latency_seconds": 0.0}
    monitor_heartbeats = 0
    cycle_started_at: datetime | None = None
    cycle_latencies: list[float] = []
    try:
        for line in runner_log_path.read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except Exception:
                continue
            timestamp = payload.get("timestamp")
            if not timestamp:
                continue
            try:
                event_time = datetime.fromisoformat(str(timestamp))
            except ValueError:
                continue
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if event_time.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") != date_label:
                continue
            if payload.get("event") == "monitor":
                monitor_heartbeats += 1
                continue
            if payload.get("event") == "cycle":
                status = str(payload.get("status", ""))
                if status == "started":
                    cycle_started_at = event_time
                elif status == "finished" and cycle_started_at is not None:
                    cycle_latencies.append(max((event_time - cycle_started_at).total_seconds(), 0.0))
                    cycle_started_at = None
    except Exception:
        return {"monitor_heartbeats": 0, "avg_decision_latency_seconds": 0.0}
    avg_latency = sum(cycle_latencies) / len(cycle_latencies) if cycle_latencies else 0.0
    return {"monitor_heartbeats": monitor_heartbeats, "avg_decision_latency_seconds": avg_latency}


def summarize_daily_records(records: list[dict[str, Any]], runner_event_counts: dict[str, int] | None = None) -> dict[str, Any]:
    runner_event_counts = runner_event_counts or {"monitor_heartbeats": 0}
    blocked_reason_counts: Counter[str] = Counter()
    rejection_reason_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    selected_symbol_counts: Counter[str] = Counter()
    executed_symbol_counts: Counter[str] = Counter()
    stage_latency_samples = {stage: [] for stage in STAGE_DISPLAY_ORDER}
    llm_wake_candidates = 0
    llm_wake_enabled = 0
    llm_wake_selected_enabled = 0
    long_proposals = 0
    short_proposals = 0
    long_accepted = 0
    short_accepted = 0
    score_totals = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
    score_counts = {"buy": 0, "sell": 0, "hold": 0}
    for item in records:
        idea = item.get("idea", {})
        approval = item.get("approval", {})
        action = str(idea.get("action", "unknown"))
        is_perp = _is_perp_record(item)
        score = _safe_float(idea.get("score"))
        action_counts[action] += 1
        if action == "buy":
            long_proposals += 1
        elif action == "sell":
            short_proposals += 1 if is_perp else 0
        if action in score_totals:
            score_totals[action] += score
            score_counts[action] += 1
        selected_symbol_counts[str(item.get("selected_symbol", "unknown"))] += 1
        if idea.get("action") != "hold" and not approval.get("approved"):
            reason = _normalize_blocked_reason(str(approval.get("reason", "unknown reason")))
            blocked_reason_counts[reason] += 1
        if _result_status(item) == "rejected":
            rejection_reason_counts[_result_reason(item)] += 1
        if _result_status(item) == "accepted":
            executed_symbol_counts[str(item.get("selected_symbol", "unknown"))] += 1
            order = _order_payload(item)
            side = str(order.get("side", "")).lower()
            reduce_only = bool(order.get("reduce_only"))
            if side == "buy" and not reduce_only:
                long_accepted += 1
            elif side == "sell" and is_perp and not reduce_only:
                short_accepted += 1
        candidates = item.get("candidates", [])
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if "llm_wake" not in candidate:
                    continue
                wake = candidate.get("llm_wake", {})
                if not isinstance(wake, dict) or "enabled" not in wake:
                    continue
                llm_wake_candidates += 1
                if wake.get("enabled"):
                    llm_wake_enabled += 1
        selected_wake = item.get("llm_wake", {})
        if isinstance(selected_wake, dict) and selected_wake.get("enabled"):
            llm_wake_selected_enabled += 1
        stage_metrics = item.get("stage_metrics", {})
        if isinstance(stage_metrics, dict):
            for stage, metrics in stage_metrics.items():
                if stage not in stage_latency_samples or not isinstance(metrics, dict):
                    continue
                stage_latency_samples[stage].append(_safe_float(metrics.get("total_seconds", 0.0)))

    total = len(records)
    proposals = sum(1 for item in records if item.get("idea", {}).get("action") != "hold")
    approved = sum(1 for item in records if item.get("approval", {}).get("approved"))
    submitted_orders = sum(1 for item in records if isinstance(item.get("result"), dict))
    accepted_orders = sum(1 for item in records if _result_status(item) == "accepted")
    rejected_orders = sum(1 for item in records if _result_status(item) == "rejected")
    executed = accepted_orders
    holds = sum(1 for item in records if item.get("idea", {}).get("action") == "hold")
    blocked = sum(
        1
        for item in records
        if item.get("idea", {}).get("action") != "hold"
        and not item.get("approval", {}).get("approved")
    )
    latest = records[-1] if records else None
    exchange_minimum_blocked = blocked_reason_counts.get("position value below exchange minimum", 0) + (
        blocked_reason_counts.get("max position below exchange minimum", 0)
    )
    avg_scores = {
        key: (score_totals[key] / score_counts[key] if score_counts[key] else 0.0)
        for key in score_totals
    }
    stage_latency_seconds = {
        stage: (sum(stage_latency_samples[stage]) / len(stage_latency_samples[stage]))
        for stage in STAGE_DISPLAY_ORDER
        if stage_latency_samples[stage]
    }
    stage_latency_p95_seconds = {
        stage: _percentile(stage_latency_samples[stage], 0.95)
        for stage in STAGE_DISPLAY_ORDER
        if stage_latency_samples[stage]
    }
    llm_wake_rate_pct = (llm_wake_enabled / llm_wake_candidates * 100.0) if llm_wake_candidates else 0.0
    llm_selected_wake_rate_pct = (llm_wake_selected_enabled / total * 100.0) if total else 0.0
    return {
        "total": total,
        "proposals": proposals,
        "approved": approved,
        "submitted_orders": submitted_orders,
        "accepted_orders": accepted_orders,
        "rejected_orders": rejected_orders,
        "executed": executed,
        "holds": holds,
        "blocked": blocked,
        "monitor_heartbeats": int(runner_event_counts.get("monitor_heartbeats", 0)),
        "avg_decision_latency_seconds": _safe_float(runner_event_counts.get("avg_decision_latency_seconds", 0.0)),
        "exchange_minimum_blocked": exchange_minimum_blocked,
        "blocked_reason_counts": dict(blocked_reason_counts.most_common()),
        "rejection_reason_counts": dict(rejection_reason_counts.most_common()),
        "action_counts": dict(action_counts.most_common()),
        "long_proposals": long_proposals,
        "short_proposals": short_proposals,
        "long_accepted": long_accepted,
        "short_accepted": short_accepted,
        "selected_symbol_counts": dict(selected_symbol_counts.most_common()),
        "executed_symbol_counts": dict(executed_symbol_counts.most_common()),
        "avg_scores": avg_scores,
        "stage_latency_seconds": stage_latency_seconds,
        "stage_latency_p95_seconds": stage_latency_p95_seconds,
        "llm_wake_candidates": llm_wake_candidates,
        "llm_wake_enabled": llm_wake_enabled,
        "llm_wake_rate_pct": llm_wake_rate_pct,
        "llm_selected_wake_enabled": llm_wake_selected_enabled,
        "llm_selected_wake_rate_pct": llm_selected_wake_rate_pct,
        "latest": latest,
    }


def load_daily_summary_data(trade_logs_dir: Path, date_label: str, runner_log_path: Path | None = None) -> dict[str, Any]:
    from trading_agents.config import load_settings

    settings = load_settings()
    records = _load_daily_records(trade_logs_dir, date_label)
    all_records = _load_all_records(trade_logs_dir)
    runner_event_counts = _load_runner_event_counts(runner_log_path, date_label)
    summary = summarize_daily_records(records, runner_event_counts)
    summary["financial_snapshot"] = _build_financial_snapshot(
        records,
        all_records,
        initial_balance_usdt=settings.initial_balance_usdt,
        taker_fee_pct=settings.taker_fee_pct,
    )
    return summary


def build_daily_summary(trade_logs_dir: Path, date_label: str, runner_log_path: Path | None = None) -> str:
    summary = load_daily_summary_data(trade_logs_dir, date_label, runner_log_path)
    total = summary["total"]
    proposals = summary["proposals"]
    approved = summary["approved"]
    executed = summary["executed"]
    submitted_orders = summary["submitted_orders"]
    rejected_orders = summary["rejected_orders"]
    holds = summary["holds"]
    blocked = summary["blocked"]
    monitor_heartbeats = summary["monitor_heartbeats"]
    avg_decision_latency_seconds = summary["avg_decision_latency_seconds"]
    exchange_minimum_blocked = summary["exchange_minimum_blocked"]
    latest = summary["latest"]
    blocked_reason_counts = summary["blocked_reason_counts"]
    financial = summary.get("financial_snapshot", {})
    avg_scores = summary.get("avg_scores", {})
    action_counts = summary.get("action_counts", {})
    executed_symbol_counts = summary.get("executed_symbol_counts", {})
    stage_latency_seconds = summary.get("stage_latency_seconds", {})
    stage_latency_p95_seconds = summary.get("stage_latency_p95_seconds", {})
    llm_wake_candidates = int(summary.get("llm_wake_candidates", 0))
    llm_wake_enabled = int(summary.get("llm_wake_enabled", 0))
    llm_wake_rate_pct = float(summary.get("llm_wake_rate_pct", 0.0))
    top_traded_symbol = next(iter(executed_symbol_counts.items()), ("n/a", 0))
    long_proposals = int(summary.get("long_proposals", 0))
    short_proposals = int(summary.get("short_proposals", 0))
    long_accepted = int(summary.get("long_accepted", 0))
    short_accepted = int(summary.get("short_accepted", 0))

    lines = [
        f"# Daily Summary: {date_label}",
        "",
        "## Financial Snapshot",
        "",
        (
            f"- Total Portfolio Value: {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT "
            f"(Initial: {float(financial.get('initial_capital_usdt', 0.0)):.2f} USDT)"
        ),
        (
            f"- Daily PnL: {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
            f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
        ),
        f"- Realized PnL: {float(financial.get('realized_pnl_usdt', 0.0)):+.2f} USDT",
        (
            f"- Realized PnL Split: "
            f"long={float(financial.get('realized_long_pnl_usdt', 0.0)):+.2f} USDT | "
            f"short={float(financial.get('realized_short_pnl_usdt', 0.0)):+.2f} USDT"
        ),
        f"- Unrealized PnL: {float(financial.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT",
        f"- Daily Fees Paid: {float(financial.get('daily_fees_usdt', 0.0)):.2f} USDT",
        f"- Cumulative Fees Paid: {float(financial.get('cumulative_fees_usdt', 0.0)):.2f} USDT",
        "",
        "## Current Portfolio",
        "",
        (
            f"- Available USDT: {float(financial.get('available_usdt', 0.0)):.2f} USDT "
            f"({100 - float(financial.get('capital_utilization_pct', 0.0)):.1f}%)"
        ),
        f"- Capital Utilization: {float(financial.get('capital_utilization_pct', 0.0)):.1f}%",
        (
            f"- Directional Exposure: "
            f"long={float(financial.get('current_long_exposure_usdt', 0.0)):.2f} USDT | "
            f"short={float(financial.get('current_short_exposure_usdt', 0.0)):.2f} USDT"
        ),
    ]

    holdings = financial.get("holdings", [])
    if holdings:
        lines.append("- Positions:")
        for item in holdings:
            if item.get("market_type") == "perp":
                lines.append(
                    f"  - {item['asset']} {item.get('position_side', 'flat')}: {float(item['quantity']):.6f} "
                    f"(Notional: {float(item['value_usdt']):.2f} USDT | Entry: {float(item.get('entry_price', 0.0)):.4f} | "
                    f"Mark: {float(item['price']):.4f} | Weight: {float(item['weight_pct']):.1f}% | "
                    f"UPnL: {float(item['unrealized_pnl_usdt']):+.2f} USDT / {float(item['unrealized_pnl_pct']):+.2f}%)"
                )
            else:
                lines.append(
                    f"  - {item['asset']}: {float(item['quantity']):.6f} "
                    f"(Val: {float(item['value_usdt']):.2f} USDT | Weight: {float(item['weight_pct']):.1f}% | "
                    f"PnL: {float(item['unrealized_pnl_usdt']):+.2f} USDT / {float(item['unrealized_pnl_pct']):+.2f}%)"
                )
    else:
        lines.append("- Positions: no tracked holdings")

    lines.extend(
        [
            "",
            "## Operations Summary",
            "",
        ]
    )

    lines.extend(
        [
            f"- Total decisions: {total}",
            f"- Monitor heartbeats: {monitor_heartbeats}",
            (
                f"- Trade proposals: {proposals} "
                f"(buy={action_counts.get('buy', 0)}, sell={action_counts.get('sell', 0)}, hold={action_counts.get('hold', 0)})"
            ),
            (
                f"- Long vs Short: "
                f"proposals long={long_proposals}, short={short_proposals} | "
                f"accepted long={long_accepted}, short={short_accepted}"
            ),
            f"- Approved by risk: {approved}",
            f"- Orders submitted: {submitted_orders}",
            f"- Executed trades: {executed}",
            f"- Rejected orders: {rejected_orders}",
            f"- Hold decisions: {holds}",
            f"- Blocked proposals: {blocked}",
            f"- Blocked by exchange minimum: {exchange_minimum_blocked}",
            f"- Avg Decision Latency: {avg_decision_latency_seconds:.2f} seconds",
            f"- Latency Breakdown Avg: {_format_stage_latency_breakdown(stage_latency_seconds)}",
            f"- Latency Breakdown P95: {_format_stage_latency_breakdown(stage_latency_p95_seconds)}",
            f"- LLM Wake Rate: {llm_wake_enabled}/{llm_wake_candidates} candidates ({llm_wake_rate_pct:.1f}%)",
            (
                f"- Agent Confidence Distribution: "
                f"buy={float(avg_scores.get('buy', 0.0)):.2f} | "
                f"sell={float(avg_scores.get('sell', 0.0)):.2f} | "
                f"hold={float(avg_scores.get('hold', 0.0)):.2f}"
            ),
            f"- Top Traded Symbol: {top_traded_symbol[0]} ({top_traded_symbol[1]} accepted trades)",
        ]
    )

    if blocked_reason_counts:
        lines.extend(["", "## Why Blocked", ""])
        for reason, count in blocked_reason_counts.items():
            lines.append(f"- {reason}: {count}")

    rejection_reason_counts = summary["rejection_reason_counts"]
    if rejection_reason_counts:
        lines.extend(["", "## Why Rejected", ""])
        for reason, count in rejection_reason_counts.items():
            lines.append(f"- {reason}: {count}")

    if latest:
        lines.extend(
            [
                "",
                "## Latest Decision",
                "",
                f"- Selected Symbol: {latest.get('selected_symbol', 'n/a')}",
                f"- Conclusion: {_summary_line(latest)}",
                f"- Signal: {latest['idea']['action']} (score={float(latest['idea']['score']):.2f})",
                f"- Risk Decision: {latest['approval']['reason']}",
            ]
        )
        if latest.get("selection_summary"):
            lines.append(f"- Selection: {latest['selection_summary']}")
        llm_wake = latest.get("llm_wake") or {}
        if llm_wake:
            lines.append(
                f"- LLM Wake: {'yes' if llm_wake.get('enabled') else 'no'} "
                f"(score={llm_wake.get('score', 0)}/{llm_wake.get('required_score', 0)}, "
                f"{'; '.join(llm_wake.get('reasons', [])[:3])})"
            )
        backtest = latest.get("backtest")
        if backtest:
            lines.append(f"- Replay Test: {backtest['summary']}")
        strategy_research = latest.get("strategy_research")
        if strategy_research:
            lines.append(f"- Strategy Research: {strategy_research['summary']}")
        debate = latest.get("debate") or {}
        if debate.get("risk_feedback"):
            lines.append(f"- Debate: risk raised `{debate['risk_feedback']}` before final decision")
        account = latest.get("account")
        if account:
            if account.get("market_type") == "perp":
                account_line = (
                    f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                    f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT | "
                    f"position {account.get('position_side', 'flat')} "
                    f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']} "
                    f"@ {float(account.get('entry_price', 0.0)):.4f} | "
                    f"UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT"
                )
            else:
                account_line = (
                    f"- Account: {float(account['free_usdt']):.2f} USDT + "
                    f"{float(account['base_asset']):.6f} {account['base_symbol']}"
                )
                if account.get("dust_position"):
                    account_line += (
                        f" (dust ignored for execution: {float(account.get('dust_notional_usdt', 0.0)):.2f} USDT)"
                    )
            lines.append(account_line)
            warnings = latest.get("approval", {}).get("warnings", [])
            if warnings:
                lines.append(f"- Main Risk: {'; '.join(warnings[:2])}")

    if total == 0:
        lines.extend(["", "今天還沒有任何交易決策紀錄。"])

    return "\n".join(lines) + "\n"


def write_daily_summary(path: Path, date_label: str, content: str) -> Path:
    target = path / f"{date_label}.md"
    target.write_text(content)
    return target


def local_date_label() -> str:
    return _local_now().strftime("%Y-%m-%d")
