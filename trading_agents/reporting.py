from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Taipei")
REPORT_WINDOW_ANCHOR_HOUR_LOCAL = 12
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
    normalized = (reason or "").strip()
    lowered = normalized.lower()
    if lowered.startswith("position value below exchange minimum"):
        return "position value below exchange minimum"
    if lowered.startswith("max position below exchange minimum"):
        return "max position below exchange minimum"
    if lowered.startswith("symbol cooldown active"):
        return "symbol cooldown active"
    if lowered.startswith("expected edge below fee hurdle"):
        return "expected edge below fee hurdle"
    if lowered.startswith("fast-cycle confidence too low"):
        return "fast-cycle confidence too low"
    if lowered.startswith("score below minimum threshold"):
        return "score below minimum threshold"
    if lowered.startswith("position too close to liquidation"):
        return "position too close to liquidation"
    if lowered.startswith("leverage would exceed cap"):
        return "leverage would exceed cap"
    if lowered.startswith("insufficient usdt balance"):
        return "insufficient usdt balance"
    if lowered.startswith("no base asset available to sell"):
        return "no base asset available to sell"

    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r":\s*[-+]?\d+(?:\.\d+)?%?\s*(?:<|>|<=|>=)\s*[-+]?\d+(?:\.\d+)?%?", "", normalized)
    normalized = re.sub(r":\s*\d+(?:\.\d+)?s remaining", "", normalized)
    normalized = re.sub(r":\s*[-+]?\d+(?:\.\d+)?", "", normalized)
    return normalized or "unknown reason"


def _normalize_result_reason(reason: str) -> str:
    lowered = reason.lower()
    if "rounds to zero" in lowered:
        return "Rejected due to minimum executable size"
    if "below bybit minimum" in lowered:
        return "Rejected due to minimum executable size"
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


def _decision_source(item: dict[str, Any]) -> str:
    return str(item.get("decision_source", "unknown")).strip().lower() or "unknown"


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _epoch_to_local_iso(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(LOCAL_TZ).isoformat()


def _trade_timestamp_local(item: dict[str, Any]) -> str:
    timestamp_text = _accepted_trade_timestamp(item)
    if not timestamp_text:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp_text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ).isoformat()


def _same_open_timestamp(left: Any, right: Any, *, tolerance_seconds: float = 120.0) -> bool:
    left_dt = _parse_timestamp_local(left)
    right_dt = _parse_timestamp_local(right)
    if left_dt is not None and right_dt is not None:
        return abs((left_dt - right_dt).total_seconds()) <= tolerance_seconds
    return str(left or "").strip() != "" and str(left or "").strip() == str(right or "").strip()


def _parse_timestamp_local(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ)


def _report_window_start_local(moment: datetime) -> datetime:
    anchor = moment.replace(
        hour=int(REPORT_WINDOW_ANCHOR_HOUR_LOCAL),
        minute=0,
        second=0,
        microsecond=0,
    )
    if moment < anchor:
        anchor -= timedelta(days=1)
    return anchor


def _is_carry_in_for_window(opened_at: Any, reference_at: Any) -> bool:
    opened_dt = _parse_timestamp_local(opened_at)
    reference_dt = _parse_timestamp_local(reference_at)
    if opened_dt is None or reference_dt is None:
        return True
    return opened_dt < _report_window_start_local(reference_dt)


def _record_timestamp_local(item: dict[str, Any]) -> str:
    raw = item.get("__record_timestamp_local") or item.get("timestamp") or _accepted_trade_timestamp(item)
    parsed = _parse_timestamp_local(raw)
    return parsed.isoformat() if parsed is not None else "n/a"


def _account_position_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    account = item.get("account") or {}
    if not isinstance(account, dict):
        return {}
    symbol = str(item.get("selected_symbol", "")).strip()
    if not symbol:
        return {}
    market_type = str(account.get("market_type", _record_mode(item))).strip().lower()
    if "perp" not in market_type:
        return {}
    side = str(account.get("position_side", "flat")).strip().lower()
    net_position = _safe_float(account.get("net_position", account.get("base_asset")))
    if side not in {"long", "short"} or abs(net_position) <= 1e-12:
        side = "flat"
        net_position = 0.0
    return {
        "symbol": symbol,
        "market_type": market_type,
        "position_side": side,
        "net_position": net_position,
        "entry_price": _safe_float(account.get("entry_price")),
        "mark_price": _safe_float(account.get("mark_price")),
        "opened_at_local": str(account.get("opened_at_local", "")).strip(),
        "opened_at_epoch": _safe_float(account.get("opened_at_epoch")),
        "hold_minutes": _safe_float(account.get("hold_minutes")),
        "entry_count": max(_safe_int(account.get("entry_count"), 0), 0),
        "position_notional_usdt": abs(_safe_float(account.get("position_notional_usdt"))),
        "record_timestamp_local": _record_timestamp_local(item),
        "record_last_price": _safe_float(item.get("last_price")),
        "decision_source": _decision_source(item),
        "accepted_reduce_only": bool(_result_status(item) == "accepted" and bool(_order_payload(item).get("reduce_only"))),
    }


def _infer_unlogged_closing_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inferred: list[dict[str, Any]] = []
    previous_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_records = sorted(
        records,
        key=lambda item: _parse_timestamp_local(item.get("__record_timestamp_local") or item.get("timestamp")) or _local_now(),
    )
    for item in ordered_records:
        snapshot = _account_position_snapshot(item)
        if not snapshot:
            continue
        symbol = str(snapshot.get("symbol", "")).strip()
        if not symbol:
            continue
        previous = previous_by_symbol.get(symbol)
        current_side = str(snapshot.get("position_side", "flat")).strip().lower()
        if (
            previous
            and str(previous.get("position_side", "flat")).strip().lower() in {"long", "short"}
            and current_side == "flat"
        ):
            order = _order_payload(item)
            if not (
                (_result_status(item) == "accepted" and bool(order.get("reduce_only")))
                or bool(previous.get("accepted_reduce_only"))
            ):
                previous_side = str(previous.get("position_side", "flat")).strip().lower()
                close_price = (
                    snapshot.get("record_last_price")
                    or _safe_float(order.get("price"))
                    or _safe_float(previous.get("mark_price"))
                    or _safe_float(previous.get("entry_price"))
                )
                quantity = abs(_safe_float(previous.get("net_position")))
                notional = quantity * close_price if quantity > 0 and close_price > 0 else _safe_float(previous.get("position_notional_usdt"))
                inferred.append(
                    {
                        "mode": item.get("mode"),
                        "selected_symbol": symbol,
                        "decision_source": "account_state_inferred",
                        "__record_timestamp_local": str(snapshot.get("record_timestamp_local", "")).strip(),
                        "idea": {
                            "rationale": "position disappeared from account state without a logged accepted close; report inferred a close from account-state transition",
                        },
                        "account": {
                            "market_type": "perp",
                            "position_side": previous_side,
                            "entry_price": _safe_float(previous.get("entry_price")),
                            "hold_minutes": _safe_float(previous.get("hold_minutes")),
                            "opened_at_local": str(previous.get("opened_at_local", "")).strip(),
                            "opened_at_epoch": _safe_float(previous.get("opened_at_epoch")),
                            "entry_count": max(_safe_int(previous.get("entry_count"), 0), 1),
                        },
                        "order": {
                            "symbol": symbol,
                            "side": "sell" if previous_side == "long" else "buy",
                            "quantity": quantity,
                            "price": close_price,
                            "notional_usdt": notional,
                            "reduce_only": True,
                        },
                        "result": {
                            "status": "accepted",
                            "timestamp": str(snapshot.get("record_timestamp_local", "")).strip(),
                            "submitted_qty": quantity,
                            "fee": 0.0,
                        },
                    }
                )
        previous_by_symbol[symbol] = snapshot
    return inferred


def _infer_unlogged_close_pnl(
    records: list[dict[str, Any]],
    *,
    taker_fee_pct: float,
) -> dict[str, float]:
    inferred_records = _infer_unlogged_closing_records(records)
    realized_long = 0.0
    realized_short = 0.0
    effective_fee_pct = min(taker_fee_pct, 0.00055)
    for item in inferred_records:
        account = item.get("account") or {}
        order = _order_payload(item)
        direction = str(account.get("position_side", "flat")).strip().lower()
        qty = _safe_float(order.get("quantity"))
        entry_price = _safe_float(account.get("entry_price"))
        close_price = _safe_float(order.get("price"))
        notional = _safe_float(order.get("notional_usdt"))
        if qty <= 0 or entry_price <= 0 or close_price <= 0 or direction not in {"long", "short"}:
            continue
        fee = notional * effective_fee_pct if notional > 0 else qty * close_price * effective_fee_pct
        pnl = ((close_price - entry_price) * qty) if direction == "long" else ((entry_price - close_price) * qty)
        if direction == "long":
            realized_long += pnl - fee
        else:
            realized_short += pnl - fee
    return {
        "long": realized_long,
        "short": realized_short,
        "records": inferred_records,
    }


def _perp_trade_label(side: str, reduce_only: bool) -> str:
    normalized = str(side or "").strip().lower()
    if reduce_only:
        return "close short" if normalized == "buy" else "close long" if normalized == "sell" else "close position"
    return "open/add long" if normalized == "buy" else "open/add short" if normalized == "sell" else normalized or "unknown"


def _load_position_policy_metadata(path: Path, mode: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    prefix = f"{str(mode or '').strip().lower()}:"
    result: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if not key.startswith(prefix):
            continue
        symbol = key[len(prefix) :].strip()
        if not symbol:
            continue
        result[symbol] = value
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _daily_strategy_review_path(trade_logs_dir: Path, date_label: str) -> Path:
    return trade_logs_dir.parent.parent / "service" / f"daily_strategy_review-{date_label}.json"


def _load_daily_strategy_review(trade_logs_dir: Path, date_label: str) -> dict[str, Any]:
    path = _daily_strategy_review_path(trade_logs_dir, date_label)
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _previous_report_date_label(date_label: str) -> str:
    window_end_date = datetime.strptime(date_label, "%Y-%m-%d").date()
    return (window_end_date - timedelta(days=1)).strftime("%Y-%m-%d")


def _estimate_avg_hold_bars(episodes: list[dict[str, Any]], timeframe_minutes: float = 15.0) -> float | None:
    if timeframe_minutes <= 0:
        return None
    values: list[float] = []
    for item in episodes:
        opened = _parse_timestamp_local(item.get("opened_at"))
        closed = _parse_timestamp_local(item.get("closed_at"))
        if opened is None or closed is None or closed <= opened:
            continue
        values.append((closed - opened).total_seconds() / 60.0 / timeframe_minutes)
    if not values:
        return None
    return fmean(values)


def _build_control_impact_summary(
    current_summary: dict[str, Any],
    previous_summary: dict[str, Any] | None,
    *,
    timeframe_minutes: float = 15.0,
) -> dict[str, Any]:
    strategy_memory = current_summary.get("strategy_memory_current") or {}
    controls = strategy_memory.get("controls") or {}
    experiment = strategy_memory.get("experiment") or {}
    if not isinstance(controls, dict):
        controls = {}
    if not isinstance(experiment, dict):
        experiment = {}
    reflection_context = strategy_memory.get("reflection_context") or {}
    previous_controls = reflection_context.get("previous_controls") or {}
    if not isinstance(previous_controls, dict):
        previous_controls = {}
    changed_controls = {
        key: {
            "previous": previous_controls.get(key),
            "current": value,
        }
        for key, value in controls.items()
        if previous_controls.get(key) != value
    }
    if not changed_controls and not experiment:
        return {}

    previous_summary = previous_summary or {}
    current_total = int(current_summary.get("total", 0) or 0)
    previous_total = int(previous_summary.get("total", 0) or 0)
    current_accepted = int(current_summary.get("accepted_orders", 0) or 0)
    previous_accepted = int(previous_summary.get("accepted_orders", 0) or 0)
    current_hold = int(current_summary.get("holds", 0) or 0)
    previous_hold = int(previous_summary.get("holds", 0) or 0)
    current_accepted_rate = (current_accepted / current_total) if current_total > 0 else 0.0
    previous_accepted_rate = (previous_accepted / previous_total) if previous_total > 0 else 0.0
    current_hold_ratio = (current_hold / current_total) if current_total > 0 else 0.0
    previous_hold_ratio = (previous_hold / previous_total) if previous_total > 0 else 0.0
    current_loss = current_summary.get("loss_attribution") or {}
    previous_loss = previous_summary.get("loss_attribution") or {}
    current_policy = current_summary.get("policy_exit_diagnostics") or {}
    previous_policy = previous_summary.get("policy_exit_diagnostics") or {}
    current_trade_review = current_summary.get("trade_review") or {}
    previous_trade_review = previous_summary.get("trade_review") or {}
    current_closed_episodes = [item for item in (current_trade_review.get("episodes") or []) if str(item.get("status", "")).lower() in {"win", "loss", "flat"}]
    previous_closed_episodes = [item for item in (previous_trade_review.get("episodes") or []) if str(item.get("status", "")).lower() in {"win", "loss", "flat"}]

    return {
        "changed_controls": changed_controls,
        "experiment": experiment,
        "accepted_rate_pct": round(current_accepted_rate * 100.0, 2),
        "accepted_rate_delta_pct": round((current_accepted_rate - previous_accepted_rate) * 100.0, 2),
        "hold_ratio_pct": round(current_hold_ratio * 100.0, 2),
        "hold_ratio_delta_pct": round((current_hold_ratio - previous_hold_ratio) * 100.0, 2),
        "cost_impact_ratio": current_loss.get("cost_impact_ratio"),
        "cost_impact_ratio_delta": (
            round(float(current_loss.get("cost_impact_ratio", 0.0)) - float(previous_loss.get("cost_impact_ratio", 0.0)), 2)
            if current_loss.get("cost_impact_ratio") is not None and previous_loss.get("cost_impact_ratio") is not None
            else None
        ),
        "accepted_policy_exit_count": int(current_policy.get("accepted_policy_exit_count", 0) or 0),
        "accepted_policy_exit_delta": int(current_policy.get("accepted_policy_exit_count", 0) or 0) - int(previous_policy.get("accepted_policy_exit_count", 0) or 0),
        "avg_hold_bars": round(_estimate_avg_hold_bars(current_closed_episodes, timeframe_minutes) or 0.0, 2),
        "avg_hold_bars_delta": (
            round(
                (float(_estimate_avg_hold_bars(current_closed_episodes, timeframe_minutes) or 0.0) -
                 float(_estimate_avg_hold_bars(previous_closed_episodes, timeframe_minutes) or 0.0)),
                2,
            )
            if current_closed_episodes or previous_closed_episodes
            else None
        ),
    }


def _build_market_path_review(records: list[dict[str, Any]], *, focus_symbol: str = "") -> dict[str, Any]:
    symbol_counts = Counter(str(item.get("selected_symbol", "")).strip() for item in records if item.get("selected_symbol"))
    symbol_counts = Counter({symbol: count for symbol, count in symbol_counts.items() if symbol})
    chosen_symbol = focus_symbol.strip() or (symbol_counts.most_common(1)[0][0] if symbol_counts else "")
    if not chosen_symbol:
        return {}

    samples: list[dict[str, Any]] = []
    for item in records:
        symbol = str(item.get("selected_symbol", "")).strip()
        if symbol != chosen_symbol:
            continue
        price = _safe_float(item.get("last_price"))
        timestamp_local = _record_timestamp_local(item)
        timestamp_dt = _parse_timestamp_local(
            item.get("__record_timestamp_local") or item.get("timestamp") or _accepted_trade_timestamp(item)
        )
        if price <= 0 or timestamp_dt is None:
            continue
        samples.append(
            {
                "timestamp_local": timestamp_local,
                "timestamp_dt": timestamp_dt,
                "price": price,
                "action": str(item.get("idea", {}).get("action", "hold")).strip().lower(),
                "source": _decision_source(item),
            }
        )
    if len(samples) < 2:
        return {}

    samples.sort(key=lambda item: item["timestamp_dt"])
    first_sample = samples[0]
    last_sample = samples[-1]
    high_sample = max(samples, key=lambda item: item["price"])
    low_sample = min(samples, key=lambda item: item["price"])
    first_price = float(first_sample["price"])
    last_price = float(last_sample["price"])
    high_price = float(high_sample["price"])
    low_price = float(low_sample["price"])
    net_move_pct = (((last_price - first_price) / first_price) * 100.0) if first_price > 0 else 0.0
    range_pct = (((high_price - low_price) / first_price) * 100.0) if first_price > 0 else 0.0

    running_peak = samples[0]
    max_drawdown_pct = 0.0
    max_drawdown_start = samples[0]
    max_drawdown_end = samples[0]
    running_trough = samples[0]
    max_rebound_pct = 0.0
    max_rebound_start = samples[0]
    max_rebound_end = samples[0]
    for sample in samples[1:]:
        if sample["price"] > running_peak["price"]:
            running_peak = sample
        drawdown_pct = ((sample["price"] - running_peak["price"]) / running_peak["price"]) * 100.0 if running_peak["price"] > 0 else 0.0
        if drawdown_pct < max_drawdown_pct:
            max_drawdown_pct = drawdown_pct
            max_drawdown_start = running_peak
            max_drawdown_end = sample

        if sample["price"] < running_trough["price"]:
            running_trough = sample
        rebound_pct = ((sample["price"] - running_trough["price"]) / running_trough["price"]) * 100.0 if running_trough["price"] > 0 else 0.0
        if rebound_pct > max_rebound_pct:
            max_rebound_pct = rebound_pct
            max_rebound_start = running_trough
            max_rebound_end = sample

    def _window_action_counts(start_dt: datetime, end_dt: datetime) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for sample in samples:
            if start_dt <= sample["timestamp_dt"] <= end_dt:
                counts[sample["action"]] += 1
        return dict(counts)

    drawdown_actions = _window_action_counts(max_drawdown_start["timestamp_dt"], max_drawdown_end["timestamp_dt"])
    rebound_actions = _window_action_counts(max_rebound_start["timestamp_dt"], max_rebound_end["timestamp_dt"])

    summary = (
        f"{chosen_symbol} 在此報告窗口的採樣盤面由 {first_sample['timestamp_local']} {first_price:.4f} "
        f"走到 {last_sample['timestamp_local']} {last_price:.4f} ({net_move_pct:+.2f}%)。"
        f" 採樣高點出現在 {high_sample['timestamp_local']} @ {high_price:.4f}，"
        f"低點出現在 {low_sample['timestamp_local']} @ {low_price:.4f}，"
        f"採樣區間約 {range_pct:.2f}%。"
    )
    if max_drawdown_pct < -0.2:
        summary += (
            f" 最大下跌段是 {max_drawdown_start['timestamp_local']} {float(max_drawdown_start['price']):.4f} "
            f"-> {max_drawdown_end['timestamp_local']} {float(max_drawdown_end['price']):.4f} "
            f"({max_drawdown_pct:+.2f}%)。"
        )
    if max_rebound_pct > 0.2:
        summary += (
            f" 最大反彈段是 {max_rebound_start['timestamp_local']} {float(max_rebound_start['price']):.4f} "
            f"-> {max_rebound_end['timestamp_local']} {float(max_rebound_end['price']):.4f} "
            f"({max_rebound_pct:+.2f}%)。"
        )

    return {
        "symbol": chosen_symbol,
        "sample_count": len(samples),
        "first_price": round(first_price, 6),
        "first_timestamp_local": str(first_sample["timestamp_local"]),
        "last_price": round(last_price, 6),
        "last_timestamp_local": str(last_sample["timestamp_local"]),
        "high_price": round(high_price, 6),
        "high_timestamp_local": str(high_sample["timestamp_local"]),
        "low_price": round(low_price, 6),
        "low_timestamp_local": str(low_sample["timestamp_local"]),
        "net_move_pct": round(net_move_pct, 4),
        "range_pct": round(range_pct, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "max_drawdown_start_local": str(max_drawdown_start["timestamp_local"]),
        "max_drawdown_end_local": str(max_drawdown_end["timestamp_local"]),
        "max_drawdown_start_price": round(float(max_drawdown_start["price"]), 6),
        "max_drawdown_end_price": round(float(max_drawdown_end["price"]), 6),
        "max_drawdown_action_counts": drawdown_actions,
        "max_rebound_pct": round(max_rebound_pct, 4),
        "max_rebound_start_local": str(max_rebound_start["timestamp_local"]),
        "max_rebound_end_local": str(max_rebound_end["timestamp_local"]),
        "max_rebound_start_price": round(float(max_rebound_start["price"]), 6),
        "max_rebound_end_price": round(float(max_rebound_end["price"]), 6),
        "max_rebound_action_counts": rebound_actions,
        "summary": summary,
    }


def _first_symbol_hint(*values: Any) -> str:
    pattern = re.compile(r"[A-Z0-9]+/[A-Z0-9]+")
    for value in values:
        if isinstance(value, dict):
            direct = str(value.get("symbol", "") or "").strip()
            if direct:
                return direct
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            if "/" in text and " " not in text and text == text.upper():
                return text
            match = pattern.search(text.upper())
            if match:
                return match.group(0)
    return ""


def _resolve_report_focus_symbol(
    *,
    settings: Any,
    records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    strategy_memory: dict[str, Any] | None = None,
    runner_status: dict[str, Any] | None = None,
) -> str:
    if len(settings.observation_pool) == 1:
        return str(settings.observation_pool[0]).strip()

    strategy_memory = strategy_memory if isinstance(strategy_memory, dict) else {}
    controls = strategy_memory.get("controls") if isinstance(strategy_memory.get("controls"), dict) else {}
    focus_symbols = strategy_memory.get("focus_symbols") if isinstance(strategy_memory.get("focus_symbols"), list) else []
    runner_status = runner_status if isinstance(runner_status, dict) else {}

    recent_symbols = [
        str(item.get("selected_symbol", "") or "").strip()
        for item in reversed(records)
        if str(item.get("selected_symbol", "") or "").strip()
    ]
    historical_symbols = [
        str(item.get("selected_symbol", "") or "").strip()
        for item in reversed(all_records)
        if str(item.get("selected_symbol", "") or "").strip()
    ]

    candidates: list[Any] = [
        *recent_symbols,
        *historical_symbols,
        controls.get("benchmark_watch_symbol"),
        controls.get("focus_symbol"),
        controls.get("live_symbol"),
        *focus_symbols,
        runner_status.get("symbol"),
        getattr(settings, "symbol", ""),
    ]
    if settings.observation_pool:
        candidates.extend(settings.observation_pool)
    return _first_symbol_hint(*candidates)


def _build_empty_market_path_review(symbol: str, reason: str) -> dict[str, Any]:
    symbol = str(symbol or "").strip()
    if not symbol:
        return {}
    return {
        "symbol": symbol,
        "sample_count": 0,
        "first_price": 0.0,
        "first_timestamp_local": "",
        "last_price": 0.0,
        "last_timestamp_local": "",
        "high_price": 0.0,
        "high_timestamp_local": "",
        "low_price": 0.0,
        "low_timestamp_local": "",
        "net_move_pct": 0.0,
        "range_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "max_drawdown_start_local": "",
        "max_drawdown_end_local": "",
        "max_drawdown_start_price": 0.0,
        "max_drawdown_end_price": 0.0,
        "max_drawdown_action_counts": {},
        "max_rebound_pct": 0.0,
        "max_rebound_start_local": "",
        "max_rebound_end_local": "",
        "max_rebound_start_price": 0.0,
        "max_rebound_end_price": 0.0,
        "max_rebound_action_counts": {},
        "summary": reason,
    }


def _annotate_market_path_coverage(
    market_path_review: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    if not market_path_review:
        return {}

    window_hours = max((window_end - window_start).total_seconds() / 3600.0, 0.0)
    sample_count = int(market_path_review.get("sample_count", 0) or 0)
    first_dt = _parse_timestamp_local(market_path_review.get("first_timestamp_local"))
    last_dt = _parse_timestamp_local(market_path_review.get("last_timestamp_local"))
    sample_span_hours = 0.0
    if first_dt is not None and last_dt is not None:
        sample_span_hours = max((last_dt - first_dt).total_seconds() / 3600.0, 0.0)
    coverage_ratio = min(sample_span_hours / window_hours, 1.0) if window_hours > 0 else 0.0

    coverage_status = "ok"
    coverage_note = ""
    if sample_count <= 0:
        coverage_status = "no_samples"
        coverage_note = (
            "runner produced no in-window decision price samples for the intended focus symbol; "
            "treat PO3, POC, VAH, VAL, and FVG path conclusions as unavailable until fresh cycles resume"
        )
    elif sample_count < 8 or coverage_ratio < 0.5:
        coverage_status = "low_coverage"
        coverage_note = (
            "runner appears to have produced only a partial decision path inside this noon window; "
            "treat PO3, POC, VAH, VAL, and FVG path conclusions as low confidence"
        )

    annotated = dict(market_path_review)
    annotated.update(
        {
            "window_hours": round(window_hours, 2),
            "sample_span_hours": round(sample_span_hours, 2),
            "coverage_ratio": round(coverage_ratio, 4),
            "coverage_status": coverage_status,
            "coverage_note": coverage_note,
        }
    )
    return annotated


def _build_symbol_postmortem(
    records: list[dict[str, Any]],
    *,
    focus_symbol: str = "",
    external_benchmarks: dict[str, Any] | None = None,
    market_path_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol_counts = Counter(str(item.get("selected_symbol", "")).strip() for item in records if item.get("selected_symbol"))
    symbol_counts = Counter({symbol: count for symbol, count in symbol_counts.items() if symbol})
    chosen_symbol = focus_symbol.strip() or (symbol_counts.most_common(1)[0][0] if symbol_counts else "")
    if not chosen_symbol:
        return {}

    symbol_records = [item for item in records if str(item.get("selected_symbol", "")).strip() == chosen_symbol]
    if not symbol_records:
        return {}

    prices = [_safe_float(item.get("last_price")) for item in symbol_records if _safe_float(item.get("last_price")) > 0]
    path = market_path_review or {}
    first_price = _safe_float(path.get("first_price")) or (prices[0] if prices else 0.0)
    last_price = _safe_float(path.get("last_price")) or (prices[-1] if prices else 0.0)
    low_price = _safe_float(path.get("low_price")) or (min(prices) if prices else 0.0)
    high_price = _safe_float(path.get("high_price")) or (max(prices) if prices else 0.0)
    net_move_pct = (((last_price - first_price) / first_price) * 100.0) if first_price > 0 else 0.0
    intraday_range_pct = _safe_float(path.get("range_pct")) or ((((high_price - low_price) / first_price) * 100.0) if first_price > 0 else 0.0)

    action_counts = Counter(str(item.get("idea", {}).get("action", "hold")).strip().lower() for item in symbol_records)
    holds = action_counts.get("hold", 0)
    sells = action_counts.get("sell", 0)
    buys = action_counts.get("buy", 0)
    proposals = buys + sells
    approved = sum(1 for item in symbol_records if bool(item.get("approval", {}).get("approved")))
    accepted = sum(1 for item in symbol_records if _result_status(item) == "accepted")
    rejected = sum(1 for item in symbol_records if _result_status(item) == "rejected")
    source_counts = Counter(_decision_source(item) for item in symbol_records)

    symbol_blocked = Counter(
        _normalize_blocked_reason(str(item.get("approval", {}).get("reason", "")))
        for item in symbol_records
        if str(item.get("idea", {}).get("action", "")).strip().lower() != "hold"
        and not bool(item.get("approval", {}).get("approved"))
    )
    symbol_blocked = Counter({reason: count for reason, count in symbol_blocked.items() if reason and reason != "unknown reason"})

    symbol_rejected = Counter(
        _result_reason(item)
        for item in symbol_records
        if _result_status(item) == "rejected"
    )
    symbol_rejected = Counter({reason: count for reason, count in symbol_rejected.items() if reason})

    top_block = next(iter(symbol_blocked.items()), ("none", 0))
    top_reject = next(iter(symbol_rejected.items()), ("none", 0))
    baseline_strategy_id = str((external_benchmarks or {}).get("baseline_strategy_id", "") or "donchian_adx_perp_v1").strip() or "donchian_adx_perp_v1"
    benchmark_payload = ((external_benchmarks or {}).get("top_by_symbol") or {}).get(chosen_symbol, {})
    benchmark_summary = ""
    if isinstance(benchmark_payload, dict) and benchmark_payload.get("candidate_id"):
        benchmark_summary = (
            f" 外部 benchmark 同標的目前以 {benchmark_payload.get('candidate_id')} 領先，"
            f"expectancy {float(benchmark_payload.get('expectancy_pct', 0.0)):+.2f}%。"
        )

    max_drawdown_pct = _safe_float(path.get("max_drawdown_pct"))
    max_rebound_pct = _safe_float(path.get("max_rebound_pct"))
    drawdown_actions = path.get("max_drawdown_action_counts") if isinstance(path.get("max_drawdown_action_counts"), dict) else {}
    rebound_actions = path.get("max_rebound_action_counts") if isinstance(path.get("max_rebound_action_counts"), dict) else {}

    regime_hint = "directional down day" if net_move_pct <= -1.0 else "directional up day" if net_move_pct >= 1.0 else "range / mixed day"
    if max_drawdown_pct <= -1.0 and int(drawdown_actions.get("sell", 0)) <= max(1, int(drawdown_actions.get("hold", 0)) // 3):
        takeaway = "採樣盤面出現明顯下跌段，但那段期間系統沒有有效轉成 sell/short，代表 long failure -> short flip 仍偏慢。"
    elif max_rebound_pct >= 1.0 and int(rebound_actions.get("buy", 0)) <= max(1, int(rebound_actions.get("hold", 0)) // 3):
        takeaway = "採樣盤面出現明顯反彈段，但那段期間系統沒有有效轉成 buy/long，代表 continuation long 仍偏保守。"
    elif net_move_pct <= -1.0 and sells <= max(1, holds // 4):
        takeaway = "價格明顯走弱，但系統大部分時間停在 hold，代表 continuation short 參與度仍不足。"
    elif net_move_pct >= 1.0 and buys <= max(1, holds // 4):
        takeaway = "價格明顯走強，但系統大部分時間停在 hold，代表 continuation long 參與度仍不足。"
    elif top_reject[0] == "Rejected due to minimum executable size" and top_reject[1] > 0:
        takeaway = "有有效訊號，但執行層仍被最小可執行單位卡掉，應持續檢查 sizing 與資金規模。"
    elif top_block[0] != "none" and top_block[1] > max(3, proposals // 3):
        takeaway = f"主要不是沒有訊號，而是大量卡在 `{top_block[0]}`。"
    else:
        takeaway = "決策與執行節奏大致一致，接下來應看進場品質與 exit timing。"

    summary = (
        f"{chosen_symbol} 今日屬於 {regime_hint}；價格由 {first_price:.4f} 走到 {last_price:.4f} "
        f"({net_move_pct:+.2f}%)，日內區間約 {intraday_range_pct:.2f}%。"
        f" 採樣高點 {path.get('high_timestamp_local', 'n/a')} @ {high_price:.4f}，"
        f"低點 {path.get('low_timestamp_local', 'n/a')} @ {low_price:.4f}。"
        f" 系統共聚焦此標的 {len(symbol_records)} 次，"
        f"buy={buys} / sell={sells} / hold={holds}，"
        f"source(base={source_counts.get('base_strategy', 0)}, fallback={source_counts.get('fallback', 0)}, "
        f"guarded={source_counts.get('fallback_guard', 0)}, memory={source_counts.get('memory_guard', 0)}, policy={source_counts.get('policy_exit', 0)})，"
        f"approved={approved}，accepted={accepted}，rejected={rejected}。"
        f" 主要 blocked 原因是 {top_block[0]} ({top_block[1]})；"
        f"主要 rejected 原因是 {top_reject[0]} ({top_reject[1]})。"
        f" {takeaway}{benchmark_summary}"
    )

    improvements: list[str] = []
    if max_drawdown_pct <= -1.0 and int(drawdown_actions.get("sell", 0)) == 0:
        improvements.append("針對明顯下跌段補強 long 失效後的快速翻空判斷，避免只會平倉卻跟不上後續跌勢。")
    if max_rebound_pct >= 1.0 and int(rebound_actions.get("buy", 0)) == 0:
        improvements.append("針對明顯反彈段補強 continuation long 或再進場邏輯，避免整段走強只剩 hold。")
    if net_move_pct <= -1.0 and holds > sells:
        improvements.append("在明顯下行日加強 continuation short 條件，避免整段趨勢中後段只剩 hold。")
    if net_move_pct >= 1.0 and holds > buys:
        improvements.append("在明顯上行日加強 continuation long 條件，避免整段趨勢中後段只剩 hold。")
    if source_counts.get("fallback", 0) > max(source_counts.get("base_strategy", 0), 1):
        improvements.append("fallback 主導次數高於 base strategy，應優先檢查 override 是否太常把 neutral day 做成方向單。")
    if top_block[0] == "symbol cooldown active":
        improvements.append("檢查單一標的模式下 cooldown 是否過長，避免同一日內趨勢段被過度冷卻。")
    if top_reject[0] == "Rejected due to minimum executable size":
        improvements.append("持續觀察最小可執行單位拒單；若仍頻繁出現，可再調整資金或單筆風險預算。")
    if benchmark_payload and benchmark_payload.get("candidate_id") and benchmark_payload.get("candidate_id") != baseline_strategy_id:
        improvements.append(
            f"外部 benchmark 顯示 `{benchmark_payload.get('candidate_id')}` 在 {chosen_symbol} 更強，應做同標的 attribution 對照。"
        )
    if not improvements:
        improvements.append("繼續追蹤這個單一標的的進場品質、持倉延續性與 policy exit 是否一致。")

    return {
        "symbol": chosen_symbol,
        "records": len(symbol_records),
        "first_price": round(first_price, 6),
        "last_price": round(last_price, 6),
        "high_price": round(high_price, 6),
        "low_price": round(low_price, 6),
        "net_move_pct": round(net_move_pct, 4),
        "intraday_range_pct": round(intraday_range_pct, 4),
        "action_counts": dict(action_counts),
        "approved": approved,
        "accepted": accepted,
        "rejected": rejected,
        "blocked_reason_counts": dict(symbol_blocked.most_common()),
        "rejection_reason_counts": dict(symbol_rejected.most_common()),
        "decision_source_counts": dict(source_counts.most_common()),
        "summary": summary,
        "improvement_directions": improvements[:4],
    }


def _accepted_trade_timestamp(item: dict[str, Any]) -> str:
    result = item.get("result") or {}
    return str(result.get("timestamp") or item.get("timestamp") or "")


def _build_trade_review(
    records: list[dict[str, Any]],
    *,
    financial_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    financial_snapshot = financial_snapshot or {}
    accepted_records = [item for item in records if _result_status(item) == "accepted"]
    inferred_close_records = _infer_unlogged_closing_records(records)
    event_records = accepted_records + inferred_close_records
    event_records.sort(
        key=lambda item: _parse_timestamp_local(_trade_timestamp_local(item)) or _parse_timestamp_local(_record_timestamp_local(item)) or _local_now(),
    )
    if not event_records:
        summary = {
            "episodes": [],
            "long_episodes": 0,
            "short_episodes": 0,
            "closed_winners": 0,
            "closed_losers": 0,
            "open_episodes": 0,
        }
        open_holdings = financial_snapshot.get("holdings") or []
        if isinstance(open_holdings, list):
            inferred_open_episodes = _open_position_episodes_from_holdings(open_holdings)
            if inferred_open_episodes:
                summary["episodes"] = inferred_open_episodes
                summary["long_episodes"] = sum(1 for item in inferred_open_episodes if item.get("direction") == "long")
                summary["short_episodes"] = sum(1 for item in inferred_open_episodes if item.get("direction") == "short")
                summary["open_episodes"] = len(inferred_open_episodes)
        return summary

    episodes: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}

    def _infer_missing_open_metadata(
        symbol: str,
        direction: str,
        opened_at: Any,
    ) -> dict[str, Any]:
        target_symbol = str(symbol or "").strip()
        target_direction = str(direction or "").strip().lower()
        if not target_symbol or target_direction not in {"long", "short"}:
            return {}
        for item in records:
            if str(item.get("selected_symbol", "")).strip() != target_symbol:
                continue
            position_context = item.get("position_context")
            account = item.get("account")
            if not isinstance(position_context, dict) or not isinstance(account, dict):
                continue
            context_side = str(position_context.get("position_side", "")).strip().lower()
            account_side = str(account.get("position_side", "")).strip().lower()
            if context_side != target_direction and account_side != target_direction:
                continue
            context_opened_at = str(position_context.get("opened_at_local", "")).strip()
            account_opened_at = str(account.get("opened_at_local", "")).strip()
            candidate_opened_at = context_opened_at or account_opened_at
            if opened_at and candidate_opened_at and not _same_open_timestamp(candidate_opened_at, opened_at):
                continue
            entry_count = _safe_int(position_context.get("entry_count"))
            if entry_count <= 0:
                entry_count = _safe_int(account.get("entry_count"))
            if entry_count <= 0:
                entry_count = 1
            return {
                "opened_at": candidate_opened_at or str(opened_at or "").strip(),
                "entry_count": entry_count,
                "decision_source": _decision_source(item),
                "entry_reason": (
                    str(item.get("idea", {}).get("rationale", "")).strip()
                    or "report inferred the opening episode from persisted position metadata on decision logs"
                ),
            }
        return {}

    def _carry_in_episode(symbol: str, close_record: dict[str, Any]) -> dict[str, Any]:
        order = _order_payload(close_record)
        account = close_record.get("account") or {}
        side = str(order.get("side", "")).strip().lower()
        account_side = str(account.get("position_side", "")).strip().lower()
        if account_side in {"long", "short"}:
            direction = account_side
        else:
            direction = "long" if side == "sell" else "short"
        close_time = _trade_timestamp_local(close_record)
        if str(close_time).strip().lower() in {"", "n/a", "none"}:
            close_time = _record_timestamp_local(close_record)
        avg_entry = _safe_float(account.get("entry_price"))
        close_price = _safe_float(order.get("price")) or _safe_float(close_record.get("last_price"))
        quantity = _safe_float(order.get("quantity")) or _safe_float((close_record.get("result") or {}).get("submitted_qty"))
        hold_minutes = _safe_float(account.get("hold_minutes"))
        opened_at = str(account.get("opened_at_local", "")).strip()
        if not opened_at:
            opened_epoch = _safe_float(account.get("opened_at_epoch"))
            if opened_epoch > 0:
                opened_at = _epoch_to_local_iso(opened_epoch)
        if not opened_at and close_time and hold_minutes > 0:
            parsed_close = _parse_timestamp_local(close_time)
            if parsed_close is not None:
                opened_at = (parsed_close - timedelta(minutes=hold_minutes)).isoformat()
        inferred_open = _infer_missing_open_metadata(symbol, direction, opened_at)
        if inferred_open.get("opened_at"):
            opened_at = str(inferred_open.get("opened_at", opened_at)).strip() or opened_at
        carry_in = _is_carry_in_for_window(opened_at, close_time)
        entry_source = "carry_in" if carry_in else "unlogged_in_window"
        entry_reason = (
            "position was already open before the first accepted trade in this report window"
            if carry_in
            else "position was opened in this report window but no accepted opening trade was found in local logs"
        )
        entry_count = max(1, int(round(_safe_float(account.get("entry_count", 1)))))
        if inferred_open:
            inferred_source = str(inferred_open.get("decision_source", "")).strip().lower()
            if not carry_in and inferred_source and inferred_source != "unknown":
                entry_source = inferred_source
            entry_reason = str(inferred_open.get("entry_reason", "")).strip() or entry_reason
            entry_count = max(entry_count, _safe_int(inferred_open.get("entry_count", 1)), 1)
        edge_pct = 0.0
        if avg_entry > 0 and close_price > 0:
            if direction == "long":
                edge_pct = ((close_price - avg_entry) / avg_entry) * 100.0
            else:
                edge_pct = ((avg_entry - close_price) / avg_entry) * 100.0
        return {
            "symbol": symbol,
            "direction": direction,
            "opened_at": opened_at or "carry-in from prior records",
            "entries": entry_count,
            "entry_quantity": quantity,
            "avg_entry_price": avg_entry,
            "entry_source": entry_source,
            "latest_entry_reason": entry_reason,
            "carry_in": carry_in,
            "closed_at": close_time,
            "close_price": round(close_price, 6),
            "estimated_edge_pct": round(edge_pct, 4),
            "status": "win" if edge_pct > 0 else "loss" if edge_pct < 0 else "flat",
            "close_reason": str(close_record.get("idea", {}).get("rationale", "")).strip(),
            "close_source": _decision_source(close_record),
        }

    def close_episode(symbol: str, close_record: dict[str, Any]) -> None:
        episode = active.pop(symbol, None)
        if not episode:
            episodes.append(_carry_in_episode(symbol, close_record))
            return
        order = _order_payload(close_record)
        close_price = _safe_float(order.get("price")) or _safe_float(close_record.get("last_price"))
        close_time = _trade_timestamp_local(close_record)
        direction = episode["direction"]
        avg_entry = _safe_float(episode.get("avg_entry_price"))
        edge_pct = 0.0
        if avg_entry > 0 and close_price > 0:
            if direction == "long":
                edge_pct = ((close_price - avg_entry) / avg_entry) * 100.0
            else:
                edge_pct = ((avg_entry - close_price) / avg_entry) * 100.0
        episode.update(
            {
                "closed_at": close_time,
                "close_price": round(close_price, 6),
                "estimated_edge_pct": round(edge_pct, 4),
                "status": "win" if edge_pct > 0 else "loss" if edge_pct < 0 else "flat",
                "close_reason": str(close_record.get("idea", {}).get("rationale", "")).strip(),
                "close_source": _decision_source(close_record),
                "carry_in": bool(episode.get("carry_in")),
            }
        )
        episodes.append(episode)

    for item in event_records:
        symbol = str(item.get("selected_symbol", "")).strip()
        if not symbol:
            continue
        order = _order_payload(item)
        side = str(order.get("side", "")).lower()
        reduce_only = bool(order.get("reduce_only"))
        quantity = _safe_float(order.get("quantity")) or _safe_float(item.get("result", {}).get("submitted_qty"))
        price = _safe_float(order.get("price")) or _safe_float(item.get("last_price"))
        timestamp = _trade_timestamp_local(item)
        source = _decision_source(item)

        if reduce_only:
            close_episode(symbol, item)
            continue

        direction = "long" if side == "buy" else "short"
        existing = active.get(symbol)
        if existing and existing.get("direction") != direction:
            # Episode reconstruction inferred a direction flip before a logged reduce-only close.
            # This is reporting-level stitching, not necessarily an executor bug.
            synthetic_close = {
                **item,
                "idea": {
                    "rationale": "episode reconstruction inferred a direction flip before a logged reduce-only close",
                },
                "order": {
                    **order,
                    "price": price,
                    "reduce_only": True,
                },
            }
            close_episode(symbol, synthetic_close)
            existing = None

        if not existing:
            active[symbol] = {
                "symbol": symbol,
                "direction": direction,
                "opened_at": timestamp,
                "entries": 1,
                "entry_quantity": quantity,
                "avg_entry_price": price,
                "entry_source": source,
                "latest_entry_reason": str(item.get("idea", {}).get("rationale", "")).strip(),
                "carry_in": False,
            }
            continue

        total_qty = max(_safe_float(existing.get("entry_quantity")), 0.0) + max(quantity, 0.0)
        prev_qty = max(_safe_float(existing.get("entry_quantity")), 0.0)
        prev_avg = _safe_float(existing.get("avg_entry_price"))
        weighted_avg = ((prev_avg * prev_qty) + (price * max(quantity, 0.0))) / total_qty if total_qty > 0 else prev_avg
        existing.update(
            {
                "entries": int(existing.get("entries", 1)) + 1,
                "entry_quantity": total_qty,
                "avg_entry_price": weighted_avg,
                "entry_source": existing.get("entry_source") or source,
                "latest_entry_reason": str(item.get("idea", {}).get("rationale", "")).strip(),
            }
        )

    latest_by_symbol: dict[str, dict[str, Any]] = {}
    for item in reversed(records):
        symbol = str(item.get("selected_symbol", "")).strip()
        if symbol and symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = item
    for symbol, episode in list(active.items()):
        latest_item = latest_by_symbol.get(symbol) or {}
        last_price = _safe_float(latest_item.get("last_price")) or _safe_float((latest_item.get("account") or {}).get("mark_price"))
        avg_entry = _safe_float(episode.get("avg_entry_price"))
        edge_pct = 0.0
        if avg_entry > 0 and last_price > 0:
            if episode["direction"] == "long":
                edge_pct = ((last_price - avg_entry) / avg_entry) * 100.0
            else:
                edge_pct = ((avg_entry - last_price) / avg_entry) * 100.0
        episode.update(
            {
                "closed_at": "",
                "close_price": round(last_price, 6) if last_price > 0 else 0.0,
                "estimated_edge_pct": round(edge_pct, 4),
                "status": "open",
                "close_reason": "still open at report cutoff",
                "close_source": "",
                "carry_in": bool(episode.get("carry_in")),
            }
        )
        episodes.append(episode)

    open_holdings = financial_snapshot.get("holdings") or []
    if isinstance(open_holdings, list):
        tracked_symbols = {str(item.get("symbol", "")).strip() for item in episodes if str(item.get("symbol", "")).strip()}
        for episode in _open_position_episodes_from_holdings(open_holdings):
            if str(episode.get("symbol", "")).strip() in tracked_symbols:
                continue
            episodes.append(episode)

    episodes.sort(key=lambda item: str(item.get("opened_at", "")))
    long_episodes = sum(1 for item in episodes if item.get("direction") == "long")
    short_episodes = sum(1 for item in episodes if item.get("direction") == "short")
    closed_winners = sum(1 for item in episodes if item.get("status") == "win")
    closed_losers = sum(1 for item in episodes if item.get("status") == "loss")
    open_episodes = sum(1 for item in episodes if item.get("status") == "open")
    return {
        "episodes": episodes,
        "long_episodes": long_episodes,
        "short_episodes": short_episodes,
        "closed_winners": closed_winners,
        "closed_losers": closed_losers,
        "open_episodes": open_episodes,
    }


def _open_position_episodes_from_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        if str(item.get("market_type", "")).strip().lower() != "perp":
            continue
        direction = str(item.get("position_side", "")).strip().lower()
        if direction not in {"long", "short"}:
            continue
        quantity = abs(_safe_float(item.get("quantity")))
        if quantity <= 0:
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        opened_at = str(item.get("opened_at_local", "")).strip() or "carry-in from prior records"
        carry_in = _is_carry_in_for_window(opened_at, opened_at)
        entry_source = str(item.get("entry_source", "")).strip()
        if not entry_source:
            entry_source = "carry_in_unlogged" if carry_in else "unlogged_in_window"
        entry_reason = str(item.get("entry_reason", "")).strip()
        if not entry_reason and entry_source == "carry_in_unlogged":
            entry_reason = "position exists in account state but no accepted opening trade was found in local logs for this window"
        elif not entry_reason and entry_source == "unlogged_in_window":
            entry_reason = "position was opened in this report window but no accepted opening trade was found in local logs"
        entry_count = max(_safe_int(item.get("entry_count", 0)), 1)
        avg_entry = _safe_float(item.get("entry_price"))
        close_or_mark = _safe_float(item.get("price"))
        edge_pct = _safe_float(item.get("unrealized_pnl_pct"))
        episodes.append(
            {
                "symbol": symbol,
                "direction": direction,
                "opened_at": opened_at,
                "entries": entry_count,
                "entry_quantity": quantity,
                "avg_entry_price": avg_entry,
                "entry_source": entry_source,
                "latest_entry_reason": entry_reason,
                "carry_in": carry_in,
                "closed_at": "",
                "close_price": round(close_or_mark, 6) if close_or_mark > 0 else 0.0,
                "estimated_edge_pct": round(edge_pct, 4),
                "status": "open",
                "close_reason": "still open at report cutoff",
                "close_source": "",
            }
        )
    return episodes


def _build_policy_exit_diagnostics(records: list[dict[str, Any]], trade_review: dict[str, Any]) -> dict[str, Any]:
    policy_records = [item for item in records if _decision_source(item) == "policy_exit"]
    accepted_policy = [item for item in policy_records if _result_status(item) == "accepted"]
    episodes = list(trade_review.get("episodes") or [])
    policy_closed = [item for item in episodes if str(item.get("close_source", "")).strip().lower() == "policy_exit"]

    stagnation_exits = 0
    max_hold_exits = 0
    end_of_day_exits = 0
    for item in policy_closed:
        reason = str(item.get("close_reason", "")).lower()
        if "stagnation exit" in reason:
            stagnation_exits += 1
        elif "hold window exceeded" in reason:
            max_hold_exits += 1
        elif "end-of-day de-risk" in reason:
            end_of_day_exits += 1

    return {
        "policy_decision_count": len(policy_records),
        "accepted_policy_exit_count": len(accepted_policy),
        "stagnation_exit_count": stagnation_exits,
        "max_hold_exit_count": max_hold_exits,
        "end_of_day_exit_count": end_of_day_exits,
        "summary": (
            f"policy decisions={len(policy_records)} | accepted exits={len(accepted_policy)} | "
            f"stagnation={stagnation_exits} | max_hold={max_hold_exits} | end_of_day={end_of_day_exits}"
        ),
    }


def _build_loss_attribution(
    records: list[dict[str, Any]],
    *,
    trade_review: dict[str, Any] | None = None,
    financial_snapshot: dict[str, Any] | None = None,
    external_benchmarks: dict[str, Any] | None = None,
    focus_symbol: str = "",
) -> dict[str, Any]:
    trade_review = trade_review or {}
    financial_snapshot = financial_snapshot or {}
    external_benchmarks = external_benchmarks or {}
    episodes = [item for item in (trade_review.get("episodes") or []) if isinstance(item, dict)]

    closed_episodes = [item for item in episodes if str(item.get("status", "")).lower() in {"win", "loss", "flat"}]
    open_episodes = [item for item in episodes if str(item.get("status", "")).lower() == "open"]
    losing_episodes = [item for item in closed_episodes if str(item.get("status", "")).lower() == "loss"]
    winning_episodes = [item for item in closed_episodes if str(item.get("status", "")).lower() == "win"]
    carry_in_closed = [item for item in closed_episodes if bool(item.get("carry_in"))]
    new_closed = [item for item in closed_episodes if not bool(item.get("carry_in"))]

    losing_by_source: Counter[str] = Counter()
    winning_by_source: Counter[str] = Counter()
    open_by_source: Counter[str] = Counter()
    losing_by_direction: Counter[str] = Counter()
    winning_by_direction: Counter[str] = Counter()
    edge_by_source: dict[str, list[float]] = {}
    edge_by_direction: dict[str, list[float]] = {"long": [], "short": []}

    for item in losing_episodes:
        source = str(item.get("entry_source", "unknown")).strip().lower() or "unknown"
        direction = str(item.get("direction", "unknown")).strip().lower() or "unknown"
        edge = _safe_float(item.get("estimated_edge_pct"))
        losing_by_source[source] += 1
        losing_by_direction[direction] += 1
        edge_by_source.setdefault(source, []).append(edge)
        edge_by_direction.setdefault(direction, []).append(edge)

    for item in winning_episodes:
        source = str(item.get("entry_source", "unknown")).strip().lower() or "unknown"
        direction = str(item.get("direction", "unknown")).strip().lower() or "unknown"
        winning_by_source[source] += 1
        winning_by_direction[direction] += 1

    for item in open_episodes:
        source = str(item.get("entry_source", "unknown")).strip().lower() or "unknown"
        open_by_source[source] += 1

    worst_episode = None
    if episodes:
        worst_episode = min(episodes, key=lambda item: _safe_float(item.get("estimated_edge_pct"), 0.0))

    accepted_source_counts: Counter[str] = Counter()
    for item in records:
        if _result_status(item) == "accepted":
            accepted_source_counts[_decision_source(item)] += 1

    focus_symbol = focus_symbol.strip()
    if not focus_symbol:
        symbol_counts = Counter(
            str(item.get("selected_symbol", "")).strip()
            for item in records
            if str(item.get("selected_symbol", "")).strip()
        )
        focus_symbol = symbol_counts.most_common(1)[0][0] if symbol_counts else ""

    symbol_benchmark = {}
    if focus_symbol:
        top_by_symbol = external_benchmarks.get("top_by_symbol") or {}
        if isinstance(top_by_symbol, dict):
            symbol_benchmark = top_by_symbol.get(focus_symbol) or {}
            if not isinstance(symbol_benchmark, dict):
                symbol_benchmark = {}

    realized_pnl = _safe_float(financial_snapshot.get("realized_pnl_usdt"))
    fees = _safe_float(financial_snapshot.get("daily_fees_usdt"))
    funding_fee = _safe_float(financial_snapshot.get("daily_funding_fee_usdt"))
    pnl_bridge_residual_usdt = _safe_float(financial_snapshot.get("pnl_bridge_residual_usdt"))
    realized_after_fees = realized_pnl - fees

    closed_edges = [_safe_float(item.get("estimated_edge_pct")) for item in closed_episodes]
    positive_edges = [edge for edge in closed_edges if edge > 0]
    negative_edges = [edge for edge in closed_edges if edge < 0]
    win_rate_pct = (len(winning_episodes) / len(closed_episodes) * 100.0) if closed_episodes else 0.0
    avg_win_edge_pct = (sum(positive_edges) / len(positive_edges)) if positive_edges else 0.0
    avg_loss_edge_pct = (sum(negative_edges) / len(negative_edges)) if negative_edges else 0.0
    expectancy_pct = (sum(closed_edges) / len(closed_edges)) if closed_edges else 0.0
    gross_profit_edge_pct = sum(positive_edges)
    gross_loss_edge_pct = abs(sum(negative_edges))
    if gross_loss_edge_pct > 0:
        live_profit_factor = gross_profit_edge_pct / gross_loss_edge_pct
    elif gross_profit_edge_pct > 0:
        live_profit_factor = None
    else:
        live_profit_factor = 0.0
    fees_and_funding = fees + funding_fee
    cost_impact_ratio = None
    if abs(realized_pnl) >= 0.05:
        cost_impact_ratio = fees_and_funding / abs(realized_pnl)
    fees_to_gross_profit_ratio = None
    if gross_profit_edge_pct > 0:
        fees_to_gross_profit_ratio = fees / gross_profit_edge_pct

    total_accepted = sum(int(value) for value in accepted_source_counts.values())
    total_losing_episodes = sum(int(value) for value in losing_by_source.values())

    primary_driver = ""
    if total_accepted == 0:
        primary_driver = "no-trade day; no validated edge passed entry filters"
    elif carry_in_closed and not new_closed and accepted_source_counts.get("policy_exit", 0) >= 1:
        primary_driver = "carry-in position was closed in this window; fees dominated the net result"
    elif accepted_source_counts.get("fallback", 0) > max(accepted_source_counts.get("base_strategy", 0), 1):
        primary_driver = "fallback dominated accepted trades"
    elif total_losing_episodes == 0 and abs(realized_after_fees) <= 0.05:
        primary_driver = "low-activity day; trading impact stayed negligible"
    elif losing_by_direction.get("long", 0) > losing_by_direction.get("short", 0):
        primary_driver = "long episodes drove most closed losses"
    elif losing_by_direction.get("short", 0) > losing_by_direction.get("long", 0):
        primary_driver = "short episodes drove most closed losses"
    elif fees > max(abs(realized_pnl), 0.01):
        primary_driver = "fees outweighed realized trading edge"
    else:
        primary_driver = "mixed execution drag across entry sources"

    observations: list[str] = []
    baseline_strategy_id = str((external_benchmarks or {}).get("baseline_strategy_id", "") or "donchian_adx_perp_v1").strip() or "donchian_adx_perp_v1"
    if total_accepted == 0:
        observations.append("no orders were accepted; today was primarily an observe-only session")
    if carry_in_closed:
        observations.append(
            f"{len(carry_in_closed)} carry-in episode(s) were closed during this report window"
        )
    if carry_in_closed and not new_closed:
        observations.append("the window's realized result came from managing an existing position, not from new entries")
    if accepted_source_counts.get("fallback", 0) > max(accepted_source_counts.get("base_strategy", 0), 1):
        observations.append("accepted trades were still fallback-heavy")
    if losing_by_direction.get("long", 0) > losing_by_direction.get("short", 0):
        observations.append("closed long episodes lost more often than shorts")
    if losing_by_direction.get("short", 0) > losing_by_direction.get("long", 0):
        observations.append("closed short episodes lost more often than longs")
    if fees > max(abs(realized_pnl), 0.01):
        observations.append("fees remained a meaningful drag versus realized PnL")
    if abs(pnl_bridge_residual_usdt) >= max(fees, 0.25):
        observations.append(
            "PnL bridge residual is elevated; carry-in or unlogged positions can make window-based equity delta diverge from lifecycle realized PnL."
        )
    if symbol_benchmark.get("candidate_id") and symbol_benchmark.get("candidate_id") != baseline_strategy_id:
        observations.append(
            f"{focus_symbol or 'focus symbol'} benchmark leader remained {symbol_benchmark.get('candidate_id')}"
        )
    if worst_episode:
        observations.append(
            f"worst episode was {worst_episode.get('symbol', 'n/a')} {worst_episode.get('direction', 'n/a')} "
            f"from {worst_episode.get('entry_source', 'unknown')} at {float(worst_episode.get('estimated_edge_pct', 0.0)):+.2f}%"
        )
    if closed_episodes:
        observations.append(
            f"closed-episode expectancy was {expectancy_pct:+.2f}% with win rate {win_rate_pct:.1f}% "
            f"({len(winning_episodes)}W/{len(losing_episodes)}L/{len(closed_episodes) - len(winning_episodes) - len(losing_episodes)} flat)"
        )
    if cost_impact_ratio is not None and cost_impact_ratio >= 0.30:
        observations.append("cost impact ratio exceeded 30%; fees are likely eating too much of the realized edge")

    avg_loss_by_source = {
        source: (sum(edges) / len(edges) if edges else 0.0)
        for source, edges in edge_by_source.items()
    }
    avg_loss_by_direction = {
        direction: (sum(edges) / len(edges) if edges else 0.0)
        for direction, edges in edge_by_direction.items()
        if edges
    }

    return {
        "primary_driver": primary_driver,
        "accepted_source_counts": dict(accepted_source_counts.most_common()),
        "carry_in_closed_count": len(carry_in_closed),
        "new_closed_count": len(new_closed),
        "losing_episode_source_counts": dict(losing_by_source.most_common()),
        "winning_episode_source_counts": dict(winning_by_source.most_common()),
        "open_episode_source_counts": dict(open_by_source.most_common()),
        "losing_episode_direction_counts": dict(losing_by_direction.most_common()),
        "winning_episode_direction_counts": dict(winning_by_direction.most_common()),
        "avg_loss_edge_by_source_pct": avg_loss_by_source,
        "avg_loss_edge_by_direction_pct": avg_loss_by_direction,
        "closed_episode_count": len(closed_episodes),
        "winning_episode_count": len(winning_episodes),
        "losing_episode_count": len(losing_episodes),
        "flat_episode_count": max(len(closed_episodes) - len(winning_episodes) - len(losing_episodes), 0),
        "live_trade_expectancy_pct": round(expectancy_pct, 4),
        "live_trade_win_rate_pct": round(win_rate_pct, 4),
        "avg_win_edge_pct": round(avg_win_edge_pct, 4),
        "avg_loss_edge_pct": round(avg_loss_edge_pct, 4),
        "gross_profit_edge_pct": round(gross_profit_edge_pct, 4),
        "gross_loss_edge_pct": round(gross_loss_edge_pct, 4),
        "live_profit_factor": round(live_profit_factor, 4) if isinstance(live_profit_factor, float) else None,
        "live_profit_factor_infinite": gross_loss_edge_pct == 0 and gross_profit_edge_pct > 0,
        "realized_after_fees_usdt": round(realized_after_fees, 4),
        "pnl_bridge_residual_usdt": round(pnl_bridge_residual_usdt, 4),
        "fees_plus_funding_usdt": round(fees_and_funding, 4),
        "funding_fee_usdt": round(funding_fee, 4),
        "cost_impact_ratio": round(cost_impact_ratio, 4) if cost_impact_ratio is not None else None,
        "cost_impact_ratio_basis": "fees+funding over absolute realized pnl" if funding_fee > 0 else "fees over absolute realized pnl (funding not integrated)",
        "fees_to_gross_profit_ratio": round(fees_to_gross_profit_ratio, 4) if fees_to_gross_profit_ratio is not None else None,
        "focus_symbol": focus_symbol,
        "focus_symbol_benchmark": symbol_benchmark,
        "worst_episode": worst_episode or {},
        "observations": observations[:5],
    }


def _build_shadow_benchmark_watch(
    external_benchmarks: dict[str, Any] | None,
    *,
    focus_symbol: str,
    watch_candidate_id: str = "",
    strategy_research_latest: dict[str, Any] | None = None,
    benchmark_reports_dir: Path | None = None,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    payload = external_benchmarks or {}
    baseline_id = str(payload.get("baseline_strategy_id", "")).strip() or "donchian_adx_perp_v1"
    focus_symbol = str(focus_symbol or "").strip()
    results_by_symbol = payload.get("results") or {}
    symbol_rows = results_by_symbol.get(focus_symbol) if isinstance(results_by_symbol, dict) and focus_symbol else None
    if not isinstance(symbol_rows, list) or not symbol_rows:
        return {
            "status": "empty",
            "focus_symbol": focus_symbol,
            "baseline_candidate_id": baseline_id,
            "watch_candidate_id": watch_candidate_id,
        }

    def _find(candidate_id: str) -> dict[str, Any]:
        for item in symbol_rows:
            if isinstance(item, dict) and str(item.get("candidate_id", "")).strip() == candidate_id:
                return item
        return {}

    baseline = _find(baseline_id)
    leader = symbol_rows[0] if symbol_rows and isinstance(symbol_rows[0], dict) else {}
    allowed_shadow_candidates = [
        "grid_range_reversion_maker_v1",
        "grid_range_reversion_v1",
        "bollinger_keltner_extreme_reversion_v1",
        "donchian_adx_keltner_v1",
        "donchian_adx_atr_midline_exit_v1",
        "bollinger_rsi_mean_reversion_v1",
        "donchian_adx_fast_14_v1",
        "donchian_adx_fast_10_v1",
    ]
    recommendation = (strategy_research_latest or {}).get("recommendation") or {}
    aggregate_ranking = list((strategy_research_latest or {}).get("aggregate_ranking") or [])
    recommended_watch_id = str(recommendation.get("candidate_id", "") or "").strip()
    recommended_verdict = str(recommendation.get("verdict", "") or "").strip().lower()
    recommended_row = _find(recommended_watch_id)
    recommended_aggregate = next(
        (
            item
            for item in aggregate_ranking
            if isinstance(item, dict) and str(item.get("candidate_id", "") or "").strip() == recommended_watch_id
        ),
        {},
    )

    requested_watch = str(watch_candidate_id or "").strip()
    selection_source = "requested"
    leader_id = str(leader.get("candidate_id", "")).strip()
    baseline_positive_edge = (
        float(baseline.get("expectancy_pct", 0.0) or 0.0) > 0.0
        and float(baseline.get("profit_factor", 0.0) or 0.0) > 1.0
    )
    if (
        not requested_watch
        and recommended_watch_id
        and recommended_watch_id == baseline_id
        and recommended_verdict in {"shadow_candidate", "promotion_candidate"}
        and leader_id == baseline_id
        and baseline_positive_edge
    ):
        return {
            "status": "baseline_confirmed",
            "focus_symbol": focus_symbol,
            "baseline_candidate_id": baseline_id,
            "watch_candidate_id": baseline_id,
            "selection_source": "baseline_and_research_aligned",
            "baseline": baseline,
            "watch": baseline,
            "leader": leader,
            "expectancy_delta_pct": 0.0,
            "profit_factor_delta": 0.0,
            "cumulative_return_delta_pct": 0.0,
            "trade_count_delta": 0,
            "is_watch_leader": True,
            "promotion_streak": 0,
            "current_snapshot_qualified": False,
            "verdict": "baseline_confirmed",
            "summary": (
                f"{focus_symbol} 上，live baseline `{baseline_id}` 與 idle-time strategy research 已對齊，"
                "目前沒有更強的替代 shadow candidate 需要推進。"
            ),
            "next_step": "維持目前 live baseline，等下一輪 research 或 benchmark 再決定是否需要新的升級候選。",
        }
    if requested_watch:
        chosen_watch_id = requested_watch
    else:
        recommended_is_eligible = (
            recommended_watch_id
            and recommended_watch_id != baseline_id
            and recommended_watch_id in allowed_shadow_candidates
            and bool(recommended_row)
            and recommended_verdict in {"shadow_candidate", "promotion_candidate"}
            and int(recommended_row.get("trade_count", 0) or 0) >= 8
            and (
                float(recommended_row.get("expectancy_pct", 0.0) or 0.0) > 0.0
                or float(recommended_row.get("profit_factor", 0.0) or 0.0) > 1.0
                or float(recommended_aggregate.get("avg_focus_expectancy_pct", 0.0) or 0.0) > 0.0
            )
        )
        if recommended_is_eligible:
            chosen_watch_id = recommended_watch_id
            selection_source = "strategy_research"
        elif (
            leader_id
            and leader_id != baseline_id
            and leader_id in allowed_shadow_candidates
            and int(leader.get("trade_count", 0) or 0) >= 8
        ):
            chosen_watch_id = leader_id
            selection_source = "external_benchmark_leader"
        else:
            chosen_watch_id = next(
                (
                    candidate_id
                    for candidate_id in allowed_shadow_candidates
                    if candidate_id != baseline_id and _find(candidate_id)
                ),
                "",
            )
            selection_source = "fallback_first_available"
    watch = _find(chosen_watch_id)
    if not baseline or not watch:
        return {
            "status": "partial",
            "focus_symbol": focus_symbol,
            "baseline_candidate_id": baseline_id,
            "watch_candidate_id": chosen_watch_id,
            "selection_source": selection_source,
            "baseline": baseline,
            "watch": watch,
            "leader": leader,
        }

    expectancy_delta = float(watch.get("expectancy_pct", 0.0)) - float(baseline.get("expectancy_pct", 0.0))
    profit_factor_delta = float(watch.get("profit_factor", 0.0)) - float(baseline.get("profit_factor", 0.0))
    cumulative_delta = float(watch.get("cumulative_return_pct", 0.0)) - float(baseline.get("cumulative_return_pct", 0.0))
    trade_count_delta = int(watch.get("trade_count", 0) or 0) - int(baseline.get("trade_count", 0) or 0)
    is_watch_leader = str(leader.get("candidate_id", "")).strip() == chosen_watch_id
    streak = _shadow_promotion_streak(
        benchmark_reports_dir,
        focus_symbol=focus_symbol,
        baseline_id=baseline_id,
        watch_candidate_id=chosen_watch_id,
        cutoff=cutoff,
    )
    current_snapshot_qualified = _shadow_snapshot_qualified(
        watch_candidate_id=chosen_watch_id,
        is_watch_leader=is_watch_leader,
        expectancy_delta=expectancy_delta,
        profit_factor_delta=profit_factor_delta,
        trade_count=int(watch.get("trade_count", 0) or 0),
        watch_expectancy_pct=float(watch.get("expectancy_pct", 0.0) or 0.0),
        watch_profit_factor=float(watch.get("profit_factor", 0.0) or 0.0),
    )
    promotion_ready = current_snapshot_qualified and streak >= 3
    watch_positive_edge = (
        float(watch.get("expectancy_pct", 0.0) or 0.0) > 0.0
        and float(watch.get("profit_factor", 0.0) or 0.0) > 1.0
    )
    verdict = (
        "promotion_candidate"
        if promotion_ready
        else "watch_streak_building"
        if current_snapshot_qualified
        else "keep_shadow_watch"
        if is_watch_leader and expectancy_delta > 0 and watch_positive_edge
        else "research_only_cost_limited"
        if is_watch_leader and expectancy_delta > 0 and not watch_positive_edge
        else "no_upgrade_signal"
    )
    next_step = (
        f"將 `{chosen_watch_id}` 納入更正式的 shadow-vs-live 追蹤，並觀察是否連續多次 benchmark 快照維持領先。"
        if verdict == "keep_shadow_watch"
        else f"`{chosen_watch_id}` 已連續 {streak} 次達到升級門檻，下一步應定義 live promotion gate。"
        if verdict == "promotion_candidate"
        else f"`{chosen_watch_id}` 已開始累積升級 streak（目前 {streak} 次），先維持 shadow 並續看下一輪 benchmark。"
        if verdict == "watch_streak_building"
        else f"`{chosen_watch_id}` 目前只是相對領先，但扣完成本後仍未達正 edge；維持 research-only，不要升成 live。"
        if verdict == "research_only_cost_limited"
        else "目前仍以 live baseline 為主，繼續觀察其他候選是否出現更穩定優勢。"
    )
    summary = (
        f"{focus_symbol} 上，shadow 候選 `{chosen_watch_id}` 對 live baseline `{baseline_id}` "
        f"的 expectancy 差值為 {expectancy_delta:+.2f}% 、profit factor 差值為 {profit_factor_delta:+.2f}，"
        f"trade count 差值 {trade_count_delta:+d}。"
    )
    return {
        "status": "ready",
        "focus_symbol": focus_symbol,
        "baseline_candidate_id": baseline_id,
        "watch_candidate_id": chosen_watch_id,
        "selection_source": selection_source,
        "baseline": baseline,
        "watch": watch,
        "leader": leader,
        "expectancy_delta_pct": round(expectancy_delta, 4),
        "profit_factor_delta": round(profit_factor_delta, 4),
        "cumulative_return_delta_pct": round(cumulative_delta, 4),
        "trade_count_delta": trade_count_delta,
        "is_watch_leader": is_watch_leader,
        "promotion_streak": streak,
        "current_snapshot_qualified": current_snapshot_qualified,
        "verdict": verdict,
        "summary": summary,
        "next_step": next_step,
    }


def _shadow_promotion_streak(
    benchmark_reports_dir: Path | None,
    *,
    focus_symbol: str,
    baseline_id: str,
    watch_candidate_id: str,
    cutoff: datetime | None = None,
    limit: int = 6,
) -> int:
    if benchmark_reports_dir is None or not benchmark_reports_dir.exists() or not focus_symbol:
        return 0
    files = _sorted_benchmark_report_files(benchmark_reports_dir, cutoff=cutoff)
    streak = 0
    for path in files[:limit]:
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        symbol_rows = ((payload.get("results") or {}).get(focus_symbol)) if isinstance(payload.get("results"), dict) else None
        if not isinstance(symbol_rows, list) or not symbol_rows:
            break
        leader = symbol_rows[0] if isinstance(symbol_rows[0], dict) else {}
        baseline = next((item for item in symbol_rows if isinstance(item, dict) and str(item.get("candidate_id", "")).strip() == baseline_id), {})
        watch = next((item for item in symbol_rows if isinstance(item, dict) and str(item.get("candidate_id", "")).strip() == watch_candidate_id), {})
        if not baseline or not watch:
            break
        expectancy_delta = float(watch.get("expectancy_pct", 0.0)) - float(baseline.get("expectancy_pct", 0.0))
        profit_factor_delta = float(watch.get("profit_factor", 0.0)) - float(baseline.get("profit_factor", 0.0))
        qualified = _shadow_snapshot_qualified(
            watch_candidate_id=watch_candidate_id,
            is_watch_leader=str(leader.get("candidate_id", "")).strip() == watch_candidate_id,
            expectancy_delta=expectancy_delta,
            profit_factor_delta=profit_factor_delta,
            trade_count=int(watch.get("trade_count", 0) or 0),
            watch_expectancy_pct=float(watch.get("expectancy_pct", 0.0) or 0.0),
            watch_profit_factor=float(watch.get("profit_factor", 0.0) or 0.0),
        )
        if not qualified:
            break
        streak += 1
    return streak


def _shadow_snapshot_qualified(
    *,
    watch_candidate_id: str,
    is_watch_leader: bool,
    expectancy_delta: float,
    profit_factor_delta: float,
    trade_count: int,
    watch_expectancy_pct: float = 0.0,
    watch_profit_factor: float = 0.0,
) -> bool:
    if not is_watch_leader:
        return False
    candidate_id = str(watch_candidate_id or "").strip()
    min_expectancy_delta = 0.03
    min_pf_delta = 0.20
    min_trades = 20
    if candidate_id == "grid_range_reversion_maker_v1":
        min_expectancy_delta = 0.08
        min_pf_delta = 1.00
        min_trades = 18
    elif candidate_id == "grid_range_reversion_v1":
        min_expectancy_delta = 0.05
        min_pf_delta = 0.50
        min_trades = 12
    elif candidate_id == "bollinger_keltner_extreme_reversion_v1":
        min_expectancy_delta = 0.05
        min_pf_delta = 0.50
        min_trades = 10
    min_trades = max(min_trades, 8)
    return (
        watch_expectancy_pct > 0.0
        and watch_profit_factor > 1.0
        and
        expectancy_delta >= min_expectancy_delta
        and profit_factor_delta >= min_pf_delta
        and trade_count >= min_trades
    )


def _phase_sort_key(value: str) -> tuple[int, str]:
    order = {
        "accumulation": 0,
        "manipulation_up": 1,
        "manipulation_down": 2,
        "expansion_up": 3,
        "expansion_down": 4,
        "unknown": 9,
    }
    label = str(value or "unknown").strip().lower() or "unknown"
    return (order.get(label, 8), label)


def _record_timestamp_local_dt(item: dict[str, Any]) -> datetime | None:
    for key in ("timestamp_local", "timestamp"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TZ)
    return None


def _episode_timestamp_local(item: dict[str, Any], key: str) -> datetime | None:
    raw = item.get(key)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _find_episode_po3_phase(
    episode: dict[str, Any],
    accepted_records: list[dict[str, Any]],
) -> str:
    symbol = str(episode.get("symbol", "") or "").strip()
    opened_at = _episode_timestamp_local(episode, "opened_at")
    if not symbol or opened_at is None:
        return "unknown"
    direction = str(episode.get("direction", "") or "").strip().lower()
    candidates: list[tuple[float, str]] = []
    for record in accepted_records:
        if str(record.get("selected_symbol", "") or "").strip() != symbol:
            continue
        action = str((record.get("idea") or {}).get("action", "") or "").strip().lower()
        if direction == "long" and action != "buy":
            continue
        if direction == "short" and action != "sell":
            continue
        phase = str(((record.get("market_structure") or {}).get("po3_phase_hint", "unknown")) or "unknown").strip().lower() or "unknown"
        record_ts = _record_timestamp_local_dt(record)
        if record_ts is None:
            continue
        candidates.append((abs((record_ts - opened_at).total_seconds()), phase))
    if not candidates:
        return "unknown"
    candidates.sort(key=lambda item: item[0])
    nearest_delta, nearest_phase = candidates[0]
    return nearest_phase if nearest_delta <= 3600 else "unknown"


def _build_po3_phase_performance(
    records: list[dict[str, Any]],
    trade_review: dict[str, Any] | None,
    *,
    taker_fee_pct: float,
) -> dict[str, Any]:
    phase_rows: dict[str, dict[str, Any]] = {}

    def ensure_row(phase: str) -> dict[str, Any]:
        label = str(phase or "unknown").strip().lower() or "unknown"
        row = phase_rows.get(label)
        if row is None:
            row = {
                "phase": label,
                "proposal_count": 0,
                "approved_count": 0,
                "executed_count": 0,
                "hold_count": 0,
                "selected_expectancy_total": 0.0,
                "selected_expectancy_count": 0,
                "closed_count": 0,
                "winning_count": 0,
                "losing_count": 0,
                "flat_count": 0,
                "after_fee_edge_total_pct": 0.0,
                "after_fee_edge_count": 0,
            }
            phase_rows[label] = row
        return row

    accepted_records = [item for item in records if _result_status(item) == "accepted"]
    for item in records:
        phase = str(((item.get("market_structure") or {}).get("po3_phase_hint", "unknown")) or "unknown").strip().lower() or "unknown"
        row = ensure_row(phase)
        action = str((item.get("idea") or {}).get("action", "hold") or "hold").strip().lower()
        if action == "hold":
            row["hold_count"] += 1
        else:
            row["proposal_count"] += 1
            if bool((item.get("approval") or {}).get("approved")):
                row["approved_count"] += 1
        if _result_status(item) == "accepted":
            row["executed_count"] += 1
        expectancy_pct = _safe_float((item.get("selected_strategy_backtest") or {}).get("expectancy_pct"))
        if expectancy_pct != 0.0:
            row["selected_expectancy_total"] += expectancy_pct
            row["selected_expectancy_count"] += 1

    for episode in ((trade_review or {}).get("episodes") or []):
        status = str(episode.get("status", "") or "").strip().lower()
        if status not in {"win", "loss", "flat"}:
            continue
        phase = _find_episode_po3_phase(episode, accepted_records)
        row = ensure_row(phase)
        row["closed_count"] += 1
        if status == "win":
            row["winning_count"] += 1
        elif status == "loss":
            row["losing_count"] += 1
        else:
            row["flat_count"] += 1
        edge_pct = _safe_float(episode.get("estimated_edge_pct"))
        after_fee_edge_pct = edge_pct - float(taker_fee_pct or 0.0) * 200.0
        row["after_fee_edge_total_pct"] += after_fee_edge_pct
        row["after_fee_edge_count"] += 1

    rows: list[dict[str, Any]] = []
    for phase, row in sorted(phase_rows.items(), key=lambda item: _phase_sort_key(item[0])):
        selected_expectancy_pct = (
            row["selected_expectancy_total"] / row["selected_expectancy_count"]
            if row["selected_expectancy_count"]
            else None
        )
        win_rate_pct = (
            row["winning_count"] / row["closed_count"] * 100.0
            if row["closed_count"]
            else None
        )
        expectancy_after_fees_pct = (
            row["after_fee_edge_total_pct"] / row["after_fee_edge_count"]
            if row["after_fee_edge_count"]
            else None
        )
        rows.append(
            {
                "phase": phase,
                "proposal_count": row["proposal_count"],
                "approved_count": row["approved_count"],
                "executed_count": row["executed_count"],
                "hold_count": row["hold_count"],
                "closed_count": row["closed_count"],
                "winning_count": row["winning_count"],
                "losing_count": row["losing_count"],
                "flat_count": row["flat_count"],
                "win_rate_pct": round(win_rate_pct, 2) if win_rate_pct is not None else None,
                "selected_expectancy_pct": round(selected_expectancy_pct, 4) if selected_expectancy_pct is not None else None,
                "expectancy_after_fees_pct": round(expectancy_after_fees_pct, 4) if expectancy_after_fees_pct is not None else None,
            }
        )
    return {
        "rows": rows,
        "note": (
            "closed-episode metrics are approximated by matching each closed episode to the nearest accepted entry decision for the same symbol and direction"
            if rows
            else ""
        ),
    }


def _benchmark_cost_note(payload: dict[str, Any]) -> str:
    total_cost = float(payload.get("total_round_trip_cost_pct", 0.0) or 0.0)
    fee = float(payload.get("round_trip_fee_pct", 0.0) or 0.0)
    slippage = float(payload.get("round_trip_slippage_pct", 0.0) or 0.0)
    funding = float(payload.get("funding_fee_pct", 0.0) or 0.0)
    if total_cost <= 0:
        return ""
    parts = [f"round-trip cost {total_cost:.2f}%"]
    if fee > 0 or slippage > 0 or funding > 0:
        parts.append(f"fee {fee:.2f}%")
        parts.append(f"slippage {slippage:.2f}%")
        if funding > 0:
            parts.append(f"funding {funding:.2f}%")
    suffix = " | custom cost assumption" if bool(payload.get("uses_custom_cost_model", False)) else ""
    return " | ".join(parts) + suffix


def _benchmark_generated_at(payload: dict[str, Any]) -> datetime | None:
    raw = str(payload.get("generated_at", "") or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sorted_benchmark_report_files(
    benchmark_reports_dir: Path | None,
    *,
    cutoff: datetime | None = None,
) -> list[Path]:
    if benchmark_reports_dir is None or not benchmark_reports_dir.exists():
        return []
    cutoff_utc = cutoff.astimezone(timezone.utc) if cutoff is not None else None
    rows: list[tuple[datetime, Path]] = []
    for path in benchmark_reports_dir.glob("external-benchmark-*.json"):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        generated_at = _benchmark_generated_at(payload)
        if generated_at is None:
            try:
                generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if cutoff_utc is not None and generated_at > cutoff_utc:
            continue
        rows.append((generated_at, path))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in rows]


def _load_external_benchmark_summary_for_window(
    state_path: Path,
    *,
    benchmark_reports_dir: Path | None,
    window_end: datetime | None,
) -> dict[str, Any]:
    from trading_agents.external_benchmarks import load_external_benchmark_summary

    if window_end is not None:
        files = _sorted_benchmark_report_files(benchmark_reports_dir, cutoff=window_end)
        for path in files[:1]:
            try:
                payload = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
    return load_external_benchmark_summary(state_path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    ticks = "▁▂▃▄▅▆▇█"
    low = min(values)
    high = max(values)
    if abs(high - low) <= 1e-9:
        return ticks[0] * len(values)
    output = []
    for value in values:
        idx = int(round((value - low) / (high - low) * (len(ticks) - 1)))
        output.append(ticks[max(0, min(idx, len(ticks) - 1))])
    return "".join(output)


def update_equity_curve(
    *,
    history_path: Path,
    chart_path: Path,
    financial_snapshot: dict[str, Any],
    timestamp: datetime | None = None,
    min_interval_seconds: float = 300.0,
    max_points: int = 800,
) -> dict[str, Any]:
    total_value = _safe_float(financial_snapshot.get("total_portfolio_value_usdt"))
    if total_value <= 0:
        return {"status": "skipped", "reason": "no portfolio value available"}
    ts = (timestamp or _local_now()).astimezone(LOCAL_TZ)
    history = _read_jsonl(history_path)
    last = history[-1] if history else None
    now_epoch = ts.timestamp()
    should_append = True
    if last:
        last_epoch = _safe_float(last.get("timestamp_epoch"))
        last_value = _safe_float(last.get("total_portfolio_value_usdt"))
        if abs(total_value - last_value) < 0.01 and (now_epoch - last_epoch) < min_interval_seconds:
            should_append = False
    if should_append:
        history.append(
            {
                "timestamp": ts.isoformat(),
                "timestamp_epoch": now_epoch,
                "total_portfolio_value_usdt": round(total_value, 6),
                "daily_pnl_usdt": round(_safe_float(financial_snapshot.get("daily_pnl_usdt")), 6),
                "realized_pnl_usdt": round(_safe_float(financial_snapshot.get("realized_pnl_usdt")), 6),
                "unrealized_pnl_usdt": round(_safe_float(financial_snapshot.get("unrealized_pnl_usdt")), 6),
            }
        )
        history = history[-max_points:]
        _write_jsonl(history_path, history)
    svg = build_equity_curve_svg(history)
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    chart_path.write_text(svg)
    recent = history[-24:] if len(history) > 24 else history
    values = [_safe_float(item.get("total_portfolio_value_usdt")) for item in recent]
    return {
        "status": "updated",
        "history_points": len(history),
        "chart_path": str(chart_path),
        "history_path": str(history_path),
        "latest_value_usdt": round(total_value, 4),
        "min_value_usdt": round(min(values), 4) if values else round(total_value, 4),
        "max_value_usdt": round(max(values), 4) if values else round(total_value, 4),
        "sparkline": _sparkline(values),
        "recent_points": [
            {
                "timestamp": item.get("timestamp", ""),
                "value_usdt": round(_safe_float(item.get("total_portfolio_value_usdt")), 4),
            }
            for item in recent[-8:]
        ],
    }


def build_equity_curve_svg(history: list[dict[str, Any]], width: int = 960, height: int = 340) -> str:
    values = [_safe_float(item.get("total_portfolio_value_usdt")) for item in history if _safe_float(item.get("total_portfolio_value_usdt")) > 0]
    if not values:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            '<rect width="100%" height="100%" fill="#0b1320"/>'
            '<text x="40" y="60" fill="#e5edf7" font-size="24" font-family="Menlo, monospace">No equity history yet</text>'
            "</svg>"
        )
    pad_l, pad_r, pad_t, pad_b = 56, 24, 28, 42
    plot_w = max(width - pad_l - pad_r, 1)
    plot_h = max(height - pad_t - pad_b, 1)
    low = min(values)
    high = max(values)
    if abs(high - low) <= 1e-9:
        high = low + 1.0
    coords: list[tuple[float, float]] = []
    for idx, value in enumerate(values):
        x = pad_l + (idx / max(len(values) - 1, 1)) * plot_w
        y = pad_t + (1.0 - ((value - low) / (high - low))) * plot_h
        coords.append((x, y))
    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    area = " ".join([f"{pad_l:.2f},{pad_t + plot_h:.2f}", polyline, f"{pad_l + plot_w:.2f},{pad_t + plot_h:.2f}"])
    latest = values[-1]
    first = values[0]
    latest_change = latest - first
    latest_color = "#21c55d" if latest_change >= 0 else "#ef4444"
    grid_lines = []
    for step in range(5):
        y = pad_t + (plot_h * step / 4.0)
        label_value = high - ((high - low) * step / 4.0)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w}" y2="{y:.2f}" stroke="#223047" stroke-width="1" />'
            f'<text x="8" y="{y + 4:.2f}" fill="#8aa0bf" font-size="12" font-family="Menlo, monospace">{label_value:.2f}</text>'
        )
    last_x, last_y = coords[-1]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#0b1320"/>
<text x="{pad_l}" y="20" fill="#e5edf7" font-size="18" font-family="Menlo, monospace">Trading Agents Equity Curve</text>
<text x="{width - 270}" y="20" fill="{latest_color}" font-size="16" font-family="Menlo, monospace">Latest {latest:.2f} USDT ({latest_change:+.2f})</text>
{''.join(grid_lines)}
<polyline fill="rgba(59,130,246,0.18)" stroke="none" points="{area}"/>
<polyline fill="none" stroke="#60a5fa" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{polyline}"/>
<circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="5" fill="{latest_color}" stroke="#e5edf7" stroke-width="2"/>
<text x="{pad_l}" y="{height - 14}" fill="#8aa0bf" font-size="12" font-family="Menlo, monospace">Points: {len(values)}</text>
<text x="{width - 220}" y="{height - 14}" fill="#8aa0bf" font-size="12" font-family="Menlo, monospace">Range: {low:.2f} - {high:.2f} USDT</text>
</svg>'''


def load_equity_curve_summary(history_path: Path, chart_path: Path) -> dict[str, Any]:
    history = _read_jsonl(history_path)
    if not history:
        return {
            "status": "empty",
            "history_points": 0,
            "chart_path": str(chart_path),
            "sparkline": "",
            "recent_points": [],
            "min_value_usdt": 0.0,
            "max_value_usdt": 0.0,
            "latest_value_usdt": 0.0,
        }
    recent = history[-24:] if len(history) > 24 else history
    values = [_safe_float(item.get("total_portfolio_value_usdt")) for item in recent]
    latest = history[-1]
    return {
        "status": "loaded",
        "history_points": len(history),
        "chart_path": str(chart_path),
        "sparkline": _sparkline(values),
        "recent_points": [
            {
                "timestamp": item.get("timestamp", ""),
                "value_usdt": round(_safe_float(item.get("total_portfolio_value_usdt")), 4),
            }
            for item in recent[-8:]
        ],
        "min_value_usdt": round(min(values), 4) if values else 0.0,
        "max_value_usdt": round(max(values), 4) if values else 0.0,
        "latest_value_usdt": round(_safe_float(latest.get("total_portfolio_value_usdt")), 4),
    }


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


def _window_label_to_bounds(date_label: str, anchor_hour: int = REPORT_WINDOW_ANCHOR_HOUR_LOCAL) -> tuple[datetime, datetime]:
    window_end_date = datetime.strptime(date_label, "%Y-%m-%d").date()
    window_end = datetime(
        window_end_date.year,
        window_end_date.month,
        window_end_date.day,
        anchor_hour,
        0,
        0,
        tzinfo=LOCAL_TZ,
    )
    window_start = window_end - timedelta(days=1)
    return window_start, window_end


def _load_strategy_research_latest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _read_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def active_report_date_label(now: datetime | None = None, anchor_hour: int = REPORT_WINDOW_ANCHOR_HOUR_LOCAL) -> str:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else _local_now()
    anchor_today = local_now.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)
    if local_now >= anchor_today:
        return (anchor_today + timedelta(days=1)).strftime("%Y-%m-%d")
    return anchor_today.strftime("%Y-%m-%d")


def completed_report_date_label(now: datetime | None = None, anchor_hour: int = REPORT_WINDOW_ANCHOR_HOUR_LOCAL) -> str:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else _local_now()
    anchor_today = local_now.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)
    if local_now >= anchor_today:
        return anchor_today.strftime("%Y-%m-%d")
    return (anchor_today - timedelta(days=1)).strftime("%Y-%m-%d")


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
    external_benchmarks = report.get("external_benchmarks") or {}
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
    position_context = report.get("position_context") or {}
    protection_profile = report.get("protection_profile") or {}
    if account:
        if account.get("market_type") == "perp":
            opened_at_local = str(account.get("opened_at_local") or position_context.get("opened_at_local") or "").strip() or "n/a"
            lines.extend(
                [
                    (
                        f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                        f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT"
                    ),
                    (
                        f"- Current Position: {account.get('position_side', 'flat')} "
                        f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']}"
                    ),
                    f"- Position Opened: {opened_at_local}",
                    (
                        f"- Entry / Mark: {float(account.get('entry_price', 0.0)):.4f} -> "
                        f"{float(account.get('mark_price', report.get('last_price', 0.0))):.4f}"
                    ),
                    (
                        f"- Take Profit / Stop Loss: "
                        f"{float(account.get('take_profit_price', 0.0)):.4f} / "
                        f"{float(account.get('stop_loss_price', 0.0)):.4f}"
                    ),
                    (
                        f"- Protection Logic: {str(protection_profile.get('regime', 'normal'))} | "
                        f"ATR {float(protection_profile.get('atr_pct', 0.0)):.2f}% | "
                        f"range {float(protection_profile.get('range_pct', 0.0)):.2f}% | "
                        f"efficiency {float(protection_profile.get('efficiency', 0.0)):.2f}"
                    ),
                    (
                        f"- Position Risk: UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT | "
                        f"Lev {float(account.get('leverage', 0.0)):.2f}x | "
                        f"Liq {float(account.get('liq_price', 0.0)):.4f} | "
                        f"Buffer {float(account.get('liquidation_buffer_pct', 0.0)):.2f}%"
                    ),
                ]
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

    top_candidate = (external_benchmarks.get("top_candidates") or [{}])[0]
    if top_candidate and top_candidate.get("candidate_id"):
        lines.extend(["", "## External Benchmarks", ""])
        lines.append(
            f"- Top benchmark: {top_candidate.get('candidate_id')} on {top_candidate.get('symbol', 'n/a')} "
            f"(expectancy={float(top_candidate.get('expectancy_pct', 0.0)):+.2f}% | "
            f"profit_factor={float(top_candidate.get('profit_factor', 0.0)):.2f} | "
            f"trades={int(top_candidate.get('trade_count', 0))})"
        )
        top_alpha = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]
        if top_alpha and top_alpha.get("candidate_id"):
            lines.append(
                f"- Top Alpha Arena benchmark: {top_alpha.get('candidate_id')} on {top_alpha.get('symbol', 'n/a')} "
                f"(expectancy={float(top_alpha.get('expectancy_pct', 0.0)):+.2f}% | "
                f"profit_factor={float(top_alpha.get('profit_factor', 0.0)):.2f})"
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
    protection_result = report.get("protection_result")
    protection_targets = report.get("protection_targets")
    protection_profile = report.get("protection_profile") or {}
    if protection_result or protection_targets:
        lines.extend(["", "## Protection", ""])
        if protection_targets:
            lines.append(
                f"- Targets: TP {float(protection_targets.get('take_profit', 0.0)):.4f} | "
                f"SL {float(protection_targets.get('stop_loss', 0.0)):.4f} | "
                f"Trailing {float(protection_targets.get('trailing_stop', 0.0)):.4f}"
            )
        if protection_profile:
            lines.append(
                f"- Profile: {str(protection_profile.get('regime', 'normal'))} | "
                f"ATR {float(protection_profile.get('atr_pct', 0.0)):.2f}% | "
                f"range {float(protection_profile.get('range_pct', 0.0)):.2f}% | "
                f"net move {float(protection_profile.get('net_move_pct', 0.0)):.2f}% | "
                f"efficiency {float(protection_profile.get('efficiency', 0.0)):.2f}"
            )
        if protection_result:
            lines.append(f"- Result: {protection_result.get('status', 'unknown')}")
            if protection_result.get("reason"):
                lines.append(f"- Note: {protection_result['reason']}")

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
    window_start, window_end = _window_label_to_bounds(date_label)
    files = sorted(trade_logs_dir.glob("*.json"), key=_path_sort_key)
    today_files = []
    for path in files:
        timestamp = _path_timestamp(path)
        if timestamp is None:
            continue
        local_timestamp = timestamp.astimezone(LOCAL_TZ)
        if window_start <= local_timestamp < window_end:
            today_files.append(path)
    records: list[dict[str, Any]] = []
    for path in today_files:
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(payload, dict):
            timestamp = _path_timestamp(path)
            if timestamp is not None:
                payload["__record_timestamp_utc"] = timestamp.isoformat()
                payload["__record_timestamp_local"] = timestamp.astimezone(LOCAL_TZ).isoformat()
            records.append(payload)
    return records


def _load_all_records(trade_logs_dir: Path) -> list[dict[str, Any]]:
    files = sorted(trade_logs_dir.glob("*.json"), key=_path_sort_key)
    records: list[dict[str, Any]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(payload, dict):
            timestamp = _path_timestamp(path)
            if timestamp is not None:
                payload["__record_timestamp_utc"] = timestamp.isoformat()
                payload["__record_timestamp_local"] = timestamp.astimezone(LOCAL_TZ).isoformat()
            records.append(payload)
    return records


def _filter_records_by_mode(records: list[dict[str, Any]], mode: str | None) -> list[dict[str, Any]]:
    normalized_mode = str(mode or "").strip().lower()
    if not normalized_mode:
        return records
    filtered = [item for item in records if str(item.get("mode", "")).strip().lower() == normalized_mode]
    return filtered if filtered else records


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
                    "leverage": _safe_float(account.get("leverage")),
                    "liq_price": _safe_float(account.get("liq_price")),
                    "position_im_usdt": _safe_float(account.get("position_im_usdt")),
                    "position_mm_usdt": _safe_float(account.get("position_mm_usdt")),
                    "take_profit_price": _safe_float(account.get("take_profit_price")),
                    "stop_loss_price": _safe_float(account.get("stop_loss_price")),
                    "trailing_stop_distance": _safe_float(account.get("trailing_stop_distance")),
                    "liquidation_buffer_pct": _safe_float(account.get("liquidation_buffer_pct")),
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
                    "leverage": _safe_float(account.get("leverage")),
                    "liq_price": _safe_float(account.get("liq_price")),
                    "position_im_usdt": _safe_float(account.get("position_im_usdt")),
                    "position_mm_usdt": _safe_float(account.get("position_mm_usdt")),
                    "take_profit_price": _safe_float(account.get("take_profit_price")),
                    "stop_loss_price": _safe_float(account.get("stop_loss_price")),
                    "trailing_stop_distance": _safe_float(account.get("trailing_stop_distance")),
                    "liquidation_buffer_pct": _safe_float(account.get("liquidation_buffer_pct")),
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


def _stale_financial_snapshot_from_last_record(
    last_record: dict[str, Any],
    *,
    initial_balance_usdt: float,
    position_policy_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    position_policy_metadata = position_policy_metadata or {}
    latest_snapshot = _portfolio_from_record(last_record)
    latest_positions = latest_snapshot.get("positions", [])
    latest_timestamp_local = str(last_record.get("__record_timestamp_local", "")).strip()
    latest_timestamp_dt = _parse_timestamp_local(latest_timestamp_local)
    stale_age_hours = 0.0
    if latest_timestamp_dt is not None:
        stale_age_hours = max((_local_now() - latest_timestamp_dt).total_seconds() / 3600.0, 0.0)
    total_portfolio_value = _safe_float(latest_snapshot.get("total_value_usdt")) or initial_balance_usdt
    invested_value = sum(abs(_safe_float(item.get("value_usdt"))) for item in latest_positions)
    current_long_exposure = 0.0
    current_short_exposure = 0.0
    holdings: list[dict[str, Any]] = []
    for item in latest_positions:
        position_value = abs(_safe_float(item.get("value_usdt")))
        signed_quantity = _safe_float(item.get("signed_quantity"))
        unrealized_pnl = _safe_float(item.get("unrealized_pnl_usdt"))
        if abs(signed_quantity) <= 1e-12 and position_value <= 1e-9 and abs(unrealized_pnl) <= 1e-9:
            continue
        side = str(item.get("position_side", "flat"))
        if side == "long":
            current_long_exposure += position_value
        elif side == "short":
            current_short_exposure += position_value
        holdings.append(
            {
                "symbol": item.get("symbol"),
                "asset": item.get("asset"),
                "quantity": _safe_float(item.get("quantity")),
                "signed_quantity": signed_quantity,
                "price": _safe_float(item.get("price")),
                "entry_price": _safe_float(item.get("entry_price")),
                "value_usdt": position_value,
                "weight_pct": (position_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
                "unrealized_pnl_usdt": unrealized_pnl,
                "unrealized_pnl_pct": ((unrealized_pnl / position_value * 100) if position_value > 0 else 0.0),
                "position_side": side,
                "market_type": str(item.get("market_type", "spot")),
                "leverage": _safe_float(item.get("leverage")),
                "liq_price": _safe_float(item.get("liq_price")),
                "position_im_usdt": _safe_float(item.get("position_im_usdt")),
                "position_mm_usdt": _safe_float(item.get("position_mm_usdt")),
                "take_profit_price": _safe_float(item.get("take_profit_price")),
                "stop_loss_price": _safe_float(item.get("stop_loss_price")),
                "trailing_stop_distance": _safe_float(item.get("trailing_stop_distance")),
                "liquidation_buffer_pct": _safe_float(item.get("liquidation_buffer_pct")),
                "opened_at_local": _epoch_to_local_iso(
                    (position_policy_metadata.get(str(item.get("symbol", "")).strip()) or {}).get("opened_at_epoch")
                ),
                "entry_count": _safe_int(
                    (position_policy_metadata.get(str(item.get("symbol", "")).strip()) or {}).get("entry_count")
                ),
                "entry_source": "stale_runtime_snapshot",
                "entry_reason": "no records landed inside this report window; carrying forward the last known portfolio snapshot",
                "entry_trade_timestamp_local": latest_timestamp_local,
            }
        )
    holdings.sort(key=lambda item: float(item["value_usdt"]), reverse=True)
    cumulative_pnl = total_portfolio_value - initial_balance_usdt
    return {
        "initial_capital_usdt": initial_balance_usdt,
        "day_start_portfolio_value_usdt": total_portfolio_value,
        "day_start_timestamp_local": latest_timestamp_local or "n/a",
        "daily_pnl_basis": "no local records inside this window; carrying forward last known portfolio snapshot",
        "total_portfolio_value_usdt": total_portfolio_value,
        "cumulative_pnl_usdt": cumulative_pnl,
        "cumulative_pnl_pct": (cumulative_pnl / initial_balance_usdt * 100) if initial_balance_usdt > 0 else 0.0,
        "daily_pnl_usdt": 0.0,
        "daily_pnl_pct": 0.0,
        "realized_pnl_usdt": 0.0,
        "realized_long_pnl_usdt": 0.0,
        "realized_short_pnl_usdt": 0.0,
        "day_start_unrealized_pnl_usdt": sum(_safe_float(item.get("unrealized_pnl_usdt")) for item in latest_positions),
        "unrealized_pnl_usdt": sum(_safe_float(item.get("unrealized_pnl_usdt")) for item in latest_positions),
        "unrealized_change_usdt": 0.0,
        "pnl_bridge_explained_usdt": 0.0,
        "pnl_bridge_residual_usdt": 0.0,
        "daily_fees_usdt": 0.0,
        "cumulative_fees_usdt": 0.0,
        "available_usdt": _safe_float(latest_snapshot.get("free_usdt")),
        "available_balance_ratio_pct": (_safe_float(latest_snapshot.get("free_usdt")) / total_portfolio_value * 100)
        if total_portfolio_value > 0
        else 0.0,
        "capital_utilization_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
        "gross_exposure_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
        "effective_leverage": (invested_value / total_portfolio_value) if total_portfolio_value > 0 else 0.0,
        "current_long_exposure_usdt": current_long_exposure,
        "current_short_exposure_usdt": current_short_exposure,
        "holdings": holdings,
        "data_freshness_status": "stale_runtime_snapshot",
        "data_freshness_reason": "no local trade/decision records were found inside this report window",
        "last_runtime_record_timestamp_local": latest_timestamp_local,
        "stale_age_hours": round(stale_age_hours, 2),
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


def _build_executed_trade_timeline(records: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for item in records:
        if _result_status(item) != "accepted":
            continue
        order = _order_payload(item)
        result = item.get("result") or {}
        account = item.get("account") or {}
        if not isinstance(order, dict):
            continue
        side = str(order.get("side", "")).strip().lower()
        reduce_only = bool(order.get("reduce_only"))
        symbol = str(order.get("symbol") or item.get("selected_symbol") or "").strip()
        quantity = _safe_float(result.get("submitted_qty")) or _safe_float(order.get("quantity"))
        price = _safe_float(order.get("price")) or _safe_float(item.get("last_price"))
        notional = _safe_float(order.get("notional_usdt"))
        if notional <= 0 and quantity > 0 and price > 0:
            notional = quantity * price
        timeline.append(
            {
                "timestamp_local": _trade_timestamp_local(item),
                "symbol": symbol,
                "market_type": str(account.get("market_type", item.get("mode", ""))),
                "label": _perp_trade_label(side, reduce_only) if _is_perp_record(item) else side or "trade",
                "side": side,
                "reduce_only": reduce_only,
                "quantity": quantity,
                "price": price,
                "notional_usdt": notional,
                "take_profit_price": _safe_float(account.get("take_profit_price")),
                "stop_loss_price": _safe_float(account.get("stop_loss_price")),
                "decision_source": _decision_source(item),
                "approval_reason": str((item.get("approval") or {}).get("reason", "")).strip(),
                "rationale": str((item.get("idea") or {}).get("rationale", "")).strip(),
                "score": _safe_float((item.get("idea") or {}).get("score")),
            }
        )
    return timeline[-limit:]


def _latest_opening_record(
    records: list[dict[str, Any]],
    *,
    symbol: str,
    position_side: str,
) -> dict[str, Any]:
    target_symbol = str(symbol or "").strip()
    target_side = str(position_side or "").strip().lower()
    if not target_symbol or target_side not in {"long", "short"}:
        return {}
    expected_order_side = "buy" if target_side == "long" else "sell"
    for item in reversed(records):
        if _result_status(item) != "accepted":
            continue
        order = _order_payload(item)
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or item.get("selected_symbol") or "").strip() != target_symbol:
            continue
        if bool(order.get("reduce_only")):
            continue
        if str(order.get("side", "")).strip().lower() != expected_order_side:
            continue
        return item
    return {}


def _build_financial_snapshot(
    records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    *,
    initial_balance_usdt: float,
    taker_fee_pct: float,
    position_policy_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    position_policy_metadata = position_policy_metadata or {}
    if not records:
        latest_record = all_records[-1] if all_records else {}
        if latest_record:
            return _stale_financial_snapshot_from_last_record(
                latest_record,
                initial_balance_usdt=initial_balance_usdt,
                position_policy_metadata=position_policy_metadata,
            )
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
    start_timestamp_local = str(records[0].get("__record_timestamp_local", "")).strip()
    accepted_today = _accepted_trade_rows(records, taker_fee_pct)
    accepted_all = _accepted_trade_rows(all_records, taker_fee_pct)
    is_perp = any(item.get("market_type") == "perp" for item in latest_snapshot.get("positions", []))

    if is_perp:
        inferred_close_pnl = _infer_unlogged_close_pnl(records, taker_fee_pct=taker_fee_pct)
        inferred_close_records = list(inferred_close_pnl.get("records") or [])
        latest_positions = latest_snapshot.get("positions", [])
        invested_value = sum(abs(_safe_float(item.get("value_usdt"))) for item in latest_positions)
        total_portfolio_value = _safe_float(latest_snapshot.get("total_value_usdt")) or initial_balance_usdt
        start_value = _safe_float(start_snapshot.get("total_value_usdt")) or initial_balance_usdt
        unrealized_pnl = sum(_safe_float(item.get("unrealized_pnl_usdt")) for item in latest_positions)
        day_start_unrealized_pnl = sum(_safe_float(item.get("unrealized_pnl_usdt")) for item in start_snapshot.get("positions", []))
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
        realized_long_pnl += _safe_float(inferred_close_pnl.get("long"))
        realized_short_pnl += _safe_float(inferred_close_pnl.get("short"))
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
            opening_record = _latest_opening_record(
                all_records,
                symbol=str(item.get("symbol", "")).strip(),
                position_side=side,
            )
            opened_at_local = _epoch_to_local_iso(
                (position_policy_metadata.get(str(item.get("symbol", "")).strip()) or {}).get("opened_at_epoch")
            )
            carry_in = _is_carry_in_for_window(opened_at_local, opened_at_local)
            opening_source = _decision_source(opening_record) if opening_record else ("carry_in_unlogged" if carry_in else "unlogged_in_window")
            opening_reason = str((opening_record.get("idea") or {}).get("rationale", "")).strip() if opening_record else ""
            if not opening_reason and opening_source == "carry_in_unlogged":
                opening_reason = "position exists in account state but no accepted opening trade was found in local logs for this window"
            elif not opening_reason and opening_source == "unlogged_in_window":
                opening_reason = "position was opened in this report window but no accepted opening trade was found in local logs"
            opening_trade_time = _trade_timestamp_local(opening_record) if opening_record else ""
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
                    "leverage": _safe_float(item.get("leverage")),
                    "liq_price": _safe_float(item.get("liq_price")),
                    "position_im_usdt": _safe_float(item.get("position_im_usdt")),
                    "position_mm_usdt": _safe_float(item.get("position_mm_usdt")),
                    "take_profit_price": _safe_float(item.get("take_profit_price")),
                    "stop_loss_price": _safe_float(item.get("stop_loss_price")),
                    "trailing_stop_distance": _safe_float(item.get("trailing_stop_distance")),
                    "liquidation_buffer_pct": _safe_float(item.get("liquidation_buffer_pct")),
                    "opened_at_local": opened_at_local,
                    "entry_count": _safe_int(
                        (position_policy_metadata.get(str(item.get("symbol", "")).strip()) or {}).get("entry_count")
                    ),
                    "entry_source": opening_source,
                    "entry_reason": opening_reason,
                    "entry_trade_timestamp_local": opening_trade_time,
                }
            )
        holdings.sort(key=lambda item: float(item["value_usdt"]), reverse=True)
        inferred_fee_rows = _accepted_trade_rows(inferred_close_records, taker_fee_pct)
        daily_fees = sum(item["fee_usdt"] for item in accepted_today) + sum(item["fee_usdt"] for item in inferred_fee_rows)
        daily_pnl = total_portfolio_value - start_value
        cumulative_pnl = total_portfolio_value - initial_balance_usdt
        unrealized_change = unrealized_pnl - day_start_unrealized_pnl
        explained_move = realized_pnl + unrealized_change
        bridge_residual = daily_pnl - explained_move
        return {
            "initial_capital_usdt": initial_balance_usdt,
            "day_start_portfolio_value_usdt": start_value,
            "day_start_timestamp_local": start_timestamp_local,
            "daily_pnl_basis": "vs first portfolio snapshot for this report window",
            "total_portfolio_value_usdt": total_portfolio_value,
            "cumulative_pnl_usdt": cumulative_pnl,
            "cumulative_pnl_pct": (cumulative_pnl / initial_balance_usdt * 100) if initial_balance_usdt > 0 else 0.0,
            "daily_pnl_usdt": daily_pnl,
            "daily_pnl_pct": (daily_pnl / start_value * 100) if start_value > 0 else 0.0,
            "realized_pnl_usdt": realized_pnl,
            "realized_long_pnl_usdt": realized_long_pnl,
            "realized_short_pnl_usdt": realized_short_pnl,
            "day_start_unrealized_pnl_usdt": day_start_unrealized_pnl,
            "unrealized_pnl_usdt": unrealized_pnl,
            "unrealized_change_usdt": unrealized_change,
            "pnl_bridge_explained_usdt": explained_move,
            "pnl_bridge_residual_usdt": bridge_residual,
            "daily_fees_usdt": daily_fees,
            "cumulative_fees_usdt": sum(item["fee_usdt"] for item in accepted_all),
            "available_usdt": _safe_float(latest_snapshot.get("free_usdt")),
            "available_balance_ratio_pct": (_safe_float(latest_snapshot.get("free_usdt")) / total_portfolio_value * 100)
            if total_portfolio_value > 0
            else 0.0,
            "capital_utilization_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
            "gross_exposure_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
            "effective_leverage": (invested_value / total_portfolio_value) if total_portfolio_value > 0 else 0.0,
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
    explained_move = realized_pnl + unrealized_pnl
    bridge_residual = daily_pnl - explained_move

    return {
        "initial_capital_usdt": initial_balance_usdt,
        "day_start_portfolio_value_usdt": start_value,
        "day_start_timestamp_local": start_timestamp_local,
        "daily_pnl_basis": "vs first portfolio snapshot for this report window",
        "total_portfolio_value_usdt": total_portfolio_value,
        "cumulative_pnl_usdt": cumulative_pnl,
        "cumulative_pnl_pct": (cumulative_pnl / initial_balance_usdt * 100) if initial_balance_usdt > 0 else 0.0,
        "daily_pnl_usdt": daily_pnl,
        "daily_pnl_pct": (daily_pnl / start_value * 100) if start_value > 0 else 0.0,
        "realized_pnl_usdt": realized_pnl,
        "day_start_unrealized_pnl_usdt": 0.0,
        "unrealized_pnl_usdt": unrealized_pnl,
        "unrealized_change_usdt": unrealized_pnl,
        "pnl_bridge_explained_usdt": explained_move,
        "pnl_bridge_residual_usdt": bridge_residual,
        "daily_fees_usdt": daily_fees,
        "cumulative_fees_usdt": sum(item["fee_usdt"] for item in accepted_all),
        "available_usdt": cash_usdt,
        "capital_utilization_pct": (invested_value / total_portfolio_value * 100) if total_portfolio_value > 0 else 0.0,
        "holdings": holdings,
    }


def _load_runner_event_counts(runner_log_path: Path | None, date_label: str) -> dict[str, float]:
    if runner_log_path is None or not runner_log_path.exists():
        return {"monitor_heartbeats": 0, "avg_decision_latency_seconds": 0.0}
    window_start, window_end = _window_label_to_bounds(date_label)
    monitor_heartbeats = 0
    cycle_started_at: datetime | None = None
    cycle_latencies: list[float] = []
    try:
        for line in _iter_recent_log_lines(runner_log_path):
            if '"event"' not in line[:256]:
                continue
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
            local_time = event_time.astimezone(LOCAL_TZ)
            if not (window_start <= local_time < window_end):
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


def _iter_recent_log_lines(path: Path, max_bytes: int = 64 * 1024 * 1024):
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(-max_bytes, os.SEEK_END)
            handle.readline()
        for raw_line in handle:
            yield raw_line.decode("utf-8", errors="replace").rstrip("\n")


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
    llm_backend_ok = 0
    llm_backend_unavailable = 0
    llm_enabled_cycles = 0
    decision_source_counts: Counter[str] = Counter()
    accepted_source_counts: Counter[str] = Counter()
    result_status_counts: Counter[str] = Counter()
    projected_balance_blocked_while_exposed = 0
    projected_balance_blocked_while_flat = 0
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
        decision_source = _decision_source(item)
        decision_source_counts[decision_source] += 1
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
            if reason == "projected available balance too low of equity":
                account = item.get("account") or {}
                position_notional = abs(_safe_float(account.get("position_notional_usdt")))
                if position_notional > 1e-9:
                    projected_balance_blocked_while_exposed += 1
                else:
                    projected_balance_blocked_while_flat += 1
        if _result_status(item) == "rejected":
            rejection_reason_counts[_result_reason(item)] += 1
        if _result_status(item) == "accepted":
            executed_symbol_counts[str(item.get("selected_symbol", "unknown"))] += 1
            accepted_source_counts[decision_source] += 1
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
        llm_health = item.get("llm_health", {})
        if isinstance(llm_health, dict):
            if str(llm_health.get("status", "")).lower() == "ok":
                llm_backend_ok += 1
            elif str(llm_health.get("status", "")).strip():
                llm_backend_unavailable += 1
        if bool(item.get("llm_enabled_for_cycle")):
            llm_enabled_cycles += 1
        status = _result_status(item)
        if status:
            result_status_counts[status] += 1
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
        "llm_backend_ok": llm_backend_ok,
        "llm_backend_unavailable": llm_backend_unavailable,
        "llm_enabled_cycles": llm_enabled_cycles,
        "result_status_counts": dict(result_status_counts.most_common()),
        "decision_source_counts": dict(decision_source_counts.most_common()),
        "accepted_source_counts": dict(accepted_source_counts.most_common()),
        "projected_balance_blocked_while_exposed": projected_balance_blocked_while_exposed,
        "projected_balance_blocked_while_flat": projected_balance_blocked_while_flat,
        "latest": latest,
    }


def load_daily_summary_data(
    trade_logs_dir: Path,
    date_label: str,
    runner_log_path: Path | None = None,
    *,
    include_control_impact: bool = True,
    trading_mode: str | None = None,
    storage_root: str | Path | None = None,
) -> dict[str, Any]:
    from trading_agents.config import load_settings
    from trading_agents.external_benchmarks import load_external_benchmark_summary
    from trading_agents.external_ai_review import external_ai_review_path, load_external_ai_review
    from trading_agents.mentor_review import mentor_review_path
    from trading_agents.storage import build_storage_layout, mode_scoped_path, mode_storage_root

    settings = load_settings()
    effective_mode = str(trading_mode or settings.trading_mode)
    effective_root = storage_root if storage_root is not None else settings.data_root
    storage = build_storage_layout(str(mode_storage_root(effective_root, effective_mode)))
    window_start, window_end = _window_label_to_bounds(date_label)
    records = _filter_records_by_mode(_load_daily_records(trade_logs_dir, date_label), effective_mode)
    all_records = _filter_records_by_mode(_load_all_records(trade_logs_dir), effective_mode)
    runner_event_counts = _load_runner_event_counts(runner_log_path, date_label)
    position_policy_metadata = _load_position_policy_metadata(storage.position_policy_state, effective_mode)
    summary = summarize_daily_records(records, runner_event_counts)
    summary["date_label"] = date_label
    summary["window_start"] = window_start.isoformat()
    summary["window_end"] = window_end.isoformat()
    summary["mode"] = effective_mode
    summary["financial_snapshot"] = _build_financial_snapshot(
        records,
        all_records,
        initial_balance_usdt=settings.initial_balance_usdt,
        taker_fee_pct=settings.taker_fee_pct,
        position_policy_metadata=position_policy_metadata,
    )
    summary["runner_status_current"] = _read_json_file(storage.runner_status)
    summary["strategy_memory_current"] = _read_json_file(storage.strategy_memory_state)
    summary["equity_curve"] = load_equity_curve_summary(
        mode_scoped_path(storage.equity_curve_history_state, effective_mode),
        mode_scoped_path(storage.equity_curve_svg, effective_mode),
    )
    summary["external_benchmarks"] = _load_external_benchmark_summary_for_window(
        storage.external_benchmark_state,
        benchmark_reports_dir=storage.benchmark_reports,
        window_end=window_end,
    )
    summary["trade_review"] = _build_trade_review(
        records,
        financial_snapshot=summary["financial_snapshot"],
    )
    summary["po3_phase_performance"] = _build_po3_phase_performance(
        records,
        summary["trade_review"],
        taker_fee_pct=settings.taker_fee_pct,
    )
    summary["policy_exit_diagnostics"] = _build_policy_exit_diagnostics(records, summary["trade_review"])
    focus_symbol = _resolve_report_focus_symbol(
        settings=settings,
        records=records,
        all_records=all_records,
        strategy_memory=summary["strategy_memory_current"],
        runner_status=summary["runner_status_current"],
    )
    summary["focus_symbol"] = focus_symbol
    summary["market_path_review"] = _annotate_market_path_coverage(
        _build_market_path_review(records, focus_symbol=focus_symbol)
        or _build_empty_market_path_review(
            focus_symbol,
            f"no local decision-price samples were recorded for {focus_symbol} inside this noon window",
        ),
        window_start=window_start,
        window_end=window_end,
    )
    summary["symbol_postmortem"] = _build_symbol_postmortem(
        records,
        focus_symbol=focus_symbol,
        external_benchmarks=summary["external_benchmarks"],
        market_path_review=summary["market_path_review"],
    )
    summary["loss_attribution"] = _build_loss_attribution(
        records,
        trade_review=summary["trade_review"],
        financial_snapshot=summary["financial_snapshot"],
        external_benchmarks=summary["external_benchmarks"],
        focus_symbol=focus_symbol,
    )
    summary["strategy_research_latest"] = _load_strategy_research_latest(
        storage.service / "strategy_research_latest.json"
    )
    summary["shadow_benchmark_watch"] = _build_shadow_benchmark_watch(
        summary["external_benchmarks"],
        focus_symbol=focus_symbol or str(summary.get("symbol_postmortem", {}).get("symbol", "")).strip(),
        strategy_research_latest=summary["strategy_research_latest"],
        benchmark_reports_dir=storage.benchmark_reports,
        cutoff=window_end,
    )
    if isinstance(summary.get("shadow_benchmark_watch"), dict):
        shadow_payload = summary["shadow_benchmark_watch"]
        summary["benchmark_watch_candidate_current"] = (
            shadow_payload.get("watch")
            if isinstance(shadow_payload.get("watch"), dict) and str(shadow_payload.get("watch", {}).get("candidate_id", "")).strip()
            else shadow_payload.get("baseline")
            if isinstance(shadow_payload.get("baseline"), dict) and str(shadow_payload.get("baseline", {}).get("candidate_id", "")).strip()
            else {}
        ) or {}
    else:
        summary["benchmark_watch_candidate_current"] = {}
    summary["executed_trade_timeline"] = _build_executed_trade_timeline(records)
    summary["daily_strategy_review"] = _load_daily_strategy_review(trade_logs_dir, date_label)
    summary["external_ai_review"] = (
        load_external_ai_review(external_ai_review_path(storage, date_label))
        if settings.external_ai_review_enabled
        else {}
    )
    mentor_path = mentor_review_path(storage, date_label)
    summary["mentor_review"] = _read_json_file(mentor_path) if mentor_path.exists() else {}
    summary["agent_trace_archive"] = {
        "directory": str(storage.agent_traces / date_label),
    }
    summary["ground_truth_artifact"] = {
        "json_path": str(storage.ground_truth_reports / f"{date_label}.json"),
        "md_path": str(storage.ground_truth_reports / f"{date_label}.md"),
    }
    summary["oracle_postmortem_artifact"] = {
        "json_path": str(storage.oracle_postmortems / f"{date_label}.json"),
        "md_path": str(storage.oracle_postmortems / f"{date_label}.md"),
    }
    if include_control_impact:
        previous_summary = {}
        previous_label = _previous_report_date_label(date_label)
        try:
            previous_summary = load_daily_summary_data(
                trade_logs_dir,
                previous_label,
                runner_log_path,
                include_control_impact=False,
                trading_mode=effective_mode,
                storage_root=effective_root,
            )
        except Exception:
            previous_summary = {}
        summary["control_impact"] = _build_control_impact_summary(summary, previous_summary)
    else:
        summary["control_impact"] = {}
    return summary


def build_daily_summary(
    trade_logs_dir: Path,
    date_label: str,
    runner_log_path: Path | None = None,
    *,
    trading_mode: str | None = None,
    storage_root: str | Path | None = None,
) -> str:
    summary = load_daily_summary_data(
        trade_logs_dir,
        date_label,
        runner_log_path,
        trading_mode=trading_mode,
        storage_root=storage_root,
    )
    window_start, window_end = _window_label_to_bounds(date_label)
    summary_mode = str(summary.get("mode", ""))
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
    equity_curve = summary.get("equity_curve", {})
    avg_scores = summary.get("avg_scores", {})
    action_counts = summary.get("action_counts", {})
    executed_symbol_counts = summary.get("executed_symbol_counts", {})
    stage_latency_seconds = summary.get("stage_latency_seconds", {})
    stage_latency_p95_seconds = summary.get("stage_latency_p95_seconds", {})
    llm_wake_candidates = int(summary.get("llm_wake_candidates", 0))
    llm_wake_enabled = int(summary.get("llm_wake_enabled", 0))
    llm_wake_rate_pct = float(summary.get("llm_wake_rate_pct", 0.0))
    llm_backend_ok = int(summary.get("llm_backend_ok", 0) or 0)
    llm_backend_unavailable = int(summary.get("llm_backend_unavailable", 0) or 0)
    llm_enabled_cycles = int(summary.get("llm_enabled_cycles", 0) or 0)
    result_status_counts = summary.get("result_status_counts", {})
    if not isinstance(result_status_counts, dict):
        result_status_counts = {}
    decision_source_counts = summary.get("decision_source_counts", {})
    accepted_source_counts = summary.get("accepted_source_counts", {})
    trade_review = summary.get("trade_review", {})
    executed_trade_timeline = summary.get("executed_trade_timeline", [])
    policy_exit_diagnostics = summary.get("policy_exit_diagnostics", {})
    top_traded_symbol = next(iter(executed_symbol_counts.items()), ("n/a", 0))
    long_proposals = int(summary.get("long_proposals", 0))
    short_proposals = int(summary.get("short_proposals", 0))
    long_accepted = int(summary.get("long_accepted", 0))
    short_accepted = int(summary.get("short_accepted", 0))
    external_benchmarks = summary.get("external_benchmarks", {})
    top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
    top_alpha_benchmark = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]
    symbol_postmortem = summary.get("symbol_postmortem") or {}
    market_path_review = summary.get("market_path_review") or {}
    loss_attribution = summary.get("loss_attribution") or {}
    shadow_benchmark_watch = summary.get("shadow_benchmark_watch") or {}
    strategy_research_latest = summary.get("strategy_research_latest") or {}
    daily_strategy_review = summary.get("daily_strategy_review") or {}
    external_ai_review = summary.get("external_ai_review") or {}
    mentor_review = summary.get("mentor_review") or {}
    control_impact = summary.get("control_impact") or {}
    agent_trace_archive = summary.get("agent_trace_archive") or {}
    ground_truth_artifact = summary.get("ground_truth_artifact") or {}
    oracle_postmortem_artifact = summary.get("oracle_postmortem_artifact") or {}

    lines = [f"# Daily Summary: {date_label}", ""]
    if summary_mode:
        lines.extend([f"- Mode: {summary_mode}", ""])
    lines.extend([f"- Window: {window_start.isoformat()} -> {window_end.isoformat()}", ""])
    freshness_status = str(financial.get("data_freshness_status", "")).strip()
    if freshness_status:
        lines.extend(
            [
                (
                    f"- Data Freshness: {freshness_status} | "
                    f"last runtime record={str(financial.get('last_runtime_record_timestamp_local', 'n/a')) or 'n/a'} | "
                    f"age={float(financial.get('stale_age_hours', 0.0)):.2f}h"
                ),
                f"- Freshness Note: {str(financial.get('data_freshness_reason', '')).strip() or 'n/a'}",
                "",
            ]
        )
    coverage_status = str(market_path_review.get("coverage_status", "")).strip()
    if coverage_status and coverage_status != "ok":
        lines.extend(
            [
                (
                    f"- Market Data Coverage: {coverage_status} | "
                    f"samples={int(market_path_review.get('sample_count', 0) or 0)} | "
                    f"span={float(market_path_review.get('sample_span_hours', 0.0) or 0.0):.2f}h / "
                    f"{float(market_path_review.get('window_hours', 0.0) or 0.0):.2f}h "
                    f"({float(market_path_review.get('coverage_ratio', 0.0) or 0.0) * 100.0:.1f}%)"
                ),
                f"- Coverage Note: {str(market_path_review.get('coverage_note', '')).strip() or 'n/a'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Financial Snapshot",
            "",
            (
                f"- Total Portfolio Value: {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT "
                f"(Configured Initial: {float(financial.get('initial_capital_usdt', 0.0)):.2f} USDT)"
            ),
            (
                f"- Daily PnL: {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
                f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
            ),
            (
                f"- Daily PnL Basis: {float(financial.get('day_start_portfolio_value_usdt', 0.0)):.2f} USDT "
                f"at {str(financial.get('day_start_timestamp_local', 'n/a')) or 'n/a'} "
                f"({str(financial.get('daily_pnl_basis', 'vs day-start portfolio value'))})"
            ),
            (
                f"- PnL Bridge: realized {float(financial.get('realized_pnl_usdt', 0.0)):+.2f} "
                f"+ unrealized change {float(financial.get('unrealized_change_usdt', 0.0)):+.2f} "
                f"+ residual {float(financial.get('pnl_bridge_residual_usdt', 0.0)):+.2f} "
                f"= daily {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT"
            ),
            (
                f"- Window Contribution: carry-in closes={int((loss_attribution.get('carry_in_closed_count') or 0))} | "
                f"new closed episodes={int((loss_attribution.get('new_closed_count') or 0))}"
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
            (
                f"- Equity Curve: {equity_curve.get('sparkline', 'n/a')} "
                f"(range {float(equity_curve.get('min_value_usdt', 0.0)):.2f} - {float(equity_curve.get('max_value_usdt', 0.0)):.2f} USDT)"
            ),
            f"- Equity Chart: {equity_curve.get('chart_path', 'n/a')}",
            "",
            "## Current Portfolio",
            "",
        ]
    )

    is_perp_summary = any(item.get("market_type") == "perp" for item in financial.get("holdings", []))
    if is_perp_summary:
        lines.extend(
            [
                (
                    f"- Available Balance: {float(financial.get('available_usdt', 0.0)):.2f} USDT "
                    f"({float(financial.get('available_balance_ratio_pct', 0.0)):.1f}% of equity)"
                ),
                f"- Gross Exposure: {float(financial.get('gross_exposure_pct', financial.get('capital_utilization_pct', 0.0))):.1f}% of equity",
                f"- Effective Leverage: {float(financial.get('effective_leverage', 0.0)):.2f}x",
                (
                    f"- Directional Exposure: "
                    f"long={float(financial.get('current_long_exposure_usdt', 0.0)):.2f} USDT | "
                    f"short={float(financial.get('current_short_exposure_usdt', 0.0)):.2f} USDT"
                ),
            ]
        )
    else:
        lines.extend(
            [
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
        )

    holdings = financial.get("holdings", [])
    open_episode_by_symbol = {
        str(item.get("symbol", "")).strip(): item
        for item in trade_review.get("episodes", [])
        if isinstance(item, dict) and str(item.get("status", "")).strip().lower() == "open"
    }
    if holdings:
        lines.append("- Positions:")
        for item in holdings:
            if item.get("market_type") == "perp":
                symbol_key = str(item.get("symbol", "")).strip()
                open_episode = open_episode_by_symbol.get(symbol_key, {})
                lines.append(
                    f"  - {item['asset']} {item.get('position_side', 'flat')}: {float(item['quantity']):.6f} "
                    f"(Notional: {float(item['value_usdt']):.2f} USDT | Weight: {float(item['weight_pct']):.1f}%)"
                )
                lines.append(
                    f"    opened: {str(item.get('opened_at_local', 'n/a')) or 'n/a'} | "
                    f"entry: {float(item.get('entry_price', 0.0)):.4f} | "
                    f"mark: {float(item['price']):.4f} | "
                    f"entries: {int(item.get('entry_count', 0) or 0)}"
                )
                lines.append(
                    f"    TP / SL: {float(item.get('take_profit_price', 0.0)):.4f} / "
                    f"{float(item.get('stop_loss_price', 0.0)):.4f} | "
                    f"UPnL: {float(item['unrealized_pnl_usdt']):+.2f} USDT / {float(item['unrealized_pnl_pct']):+.2f}%"
                )
                lines.append(
                    f"    leverage: {float(item.get('leverage', 0.0)):.2f}x | "
                    f"liq buffer: {float(item.get('liquidation_buffer_pct', 0.0)):.2f}%"
                )
                if item.get("entry_trade_timestamp_local") or item.get("entry_source"):
                    lines.append(
                        f"    opened by: {item.get('entry_source', 'unknown') or 'unknown'} | "
                        f"trade_time: {item.get('entry_trade_timestamp_local', 'n/a') or 'n/a'}"
                    )
                if item.get("entry_reason"):
                    lines.append(f"    thesis: {item.get('entry_reason')}")
                elif open_episode.get("latest_entry_reason"):
                    lines.append(f"    thesis: {open_episode.get('latest_entry_reason')}")
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
            (
                f"- Decision Attribution: "
                f"base={int(decision_source_counts.get('base_strategy', 0))} | "
                f"fallback={int(decision_source_counts.get('fallback', 0))} | "
                f"guarded={int(decision_source_counts.get('fallback_guard', 0))} | "
                f"memory={int(decision_source_counts.get('memory_guard', 0))} | "
                f"policy={int(decision_source_counts.get('policy_exit', 0))}"
            ),
            (
                f"- Accepted Attribution: "
                f"base={int(accepted_source_counts.get('base_strategy', 0))} | "
                f"fallback={int(accepted_source_counts.get('fallback', 0))} | "
                f"guarded={int(accepted_source_counts.get('fallback_guard', 0))} | "
                f"memory={int(accepted_source_counts.get('memory_guard', 0))} | "
                f"policy={int(accepted_source_counts.get('policy_exit', 0))}"
            ),
            f"- Approved by risk: {approved}",
            f"- Orders submitted: {submitted_orders}",
            f"- Executed trades: {executed}",
            f"- Rejected orders: {rejected_orders}",
            (
                f"- Order Statuses: accepted={int(result_status_counts.get('accepted', 0))} | "
                f"filled={int(result_status_counts.get('filled', 0))} | "
                f"expired={int(result_status_counts.get('expired', 0))} | "
                f"cancelled={int(result_status_counts.get('cancelled', result_status_counts.get('canceled', 0)))} | "
                f"rejected={int(result_status_counts.get('rejected', 0))}"
            ),
            f"- Hold decisions: {holds}",
            f"- Blocked proposals: {blocked}",
            f"- Blocked by exchange minimum: {exchange_minimum_blocked}",
            f"- Avg Decision Latency: {avg_decision_latency_seconds:.2f} seconds",
            f"- Latency Breakdown Avg: {_format_stage_latency_breakdown(stage_latency_seconds)}",
            f"- Latency Breakdown P95: {_format_stage_latency_breakdown(stage_latency_p95_seconds)}",
            f"- LLM Wake Rate: {llm_wake_enabled}/{llm_wake_candidates} candidates ({llm_wake_rate_pct:.1f}%)",
            (
                f"- LLM Backend Health: ok={llm_backend_ok}, unavailable={llm_backend_unavailable}, "
                f"enabled_cycles={llm_enabled_cycles}"
            ),
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

    lines.extend(["", "## Executed Trades Today", ""])
    if executed_trade_timeline:
        for item in executed_trade_timeline:
            lines.append(
                f"- {item.get('timestamp_local', 'n/a')} | {item.get('symbol', 'n/a')} | "
                f"{item.get('label', 'trade')} | qty={float(item.get('quantity', 0.0)):.6f} | "
                f"price={float(item.get('price', 0.0)):.4f} | notional={float(item.get('notional_usdt', 0.0)):.2f} USDT | "
                f"TP={float(item.get('take_profit_price', 0.0)):.4f} | SL={float(item.get('stop_loss_price', 0.0)):.4f}"
            )
            lines.append(
                f"  source={item.get('decision_source', 'unknown')} | "
                f"score={float(item.get('score', 0.0)):.2f} | "
                f"risk={item.get('approval_reason', 'n/a')}"
            )
            if item.get("rationale"):
                lines.append(f"  why={item.get('rationale')}")
    else:
        lines.append("- No accepted trades today.")

    if top_benchmark.get("candidate_id"):
        lines.extend(["", "## External Benchmarks", ""])
        lines.append(f"- Refreshed at: {external_benchmarks.get('generated_at', 'n/a')}")
        lines.append(f"- Live baseline strategy: {external_benchmarks.get('baseline_strategy_id', 'n/a')}")
        cost_model = external_benchmarks.get("cost_model") or {}
        if isinstance(cost_model, dict) and cost_model:
            lines.append(
                f"- Cost Model: round-trip fee {float(cost_model.get('round_trip_fee_pct', 0.0)):.2f}% | "
                f"round-trip slippage {float(cost_model.get('round_trip_slippage_pct', 0.0)):.2f}% | "
                f"funding integrated={'yes' if bool(cost_model.get('funding_integrated', False)) else 'no'}"
            )
        lines.append(
            f"- Top benchmark overall: {top_benchmark.get('candidate_id')} on {top_benchmark.get('symbol', 'n/a')} "
            f"(expectancy={float(top_benchmark.get('expectancy_pct', 0.0)):+.2f}% | "
            f"profit_factor={float(top_benchmark.get('profit_factor', 0.0)):.2f} | "
            f"trades={int(top_benchmark.get('trade_count', 0))})"
        )
        if int(top_benchmark.get("trade_count", 0) or 0) < 8:
            lines.append("- Benchmark Guard: top benchmark remains low-sample (<8 trades); keep it research-only and do not use it as a promotion basis yet.")
        top_cost_note = _benchmark_cost_note(top_benchmark)
        if top_cost_note:
            lines.append(f"  cost={top_cost_note}")
        if top_alpha_benchmark.get("candidate_id"):
            lines.append(
                f"- Top Alpha Arena model: {top_alpha_benchmark.get('candidate_id')} on {top_alpha_benchmark.get('symbol', 'n/a')} "
                f"(expectancy={float(top_alpha_benchmark.get('expectancy_pct', 0.0)):+.2f}% | "
                f"profit_factor={float(top_alpha_benchmark.get('profit_factor', 0.0)):.2f})"
            )
        top_by_symbol = external_benchmarks.get("top_by_symbol", {})
        if isinstance(top_by_symbol, dict):
            for symbol_key, payload in top_by_symbol.items():
                if not isinstance(payload, dict):
                    continue
                lines.append(
                    f"- {symbol_key}: {payload.get('candidate_id', 'n/a')} "
                    f"(expectancy={float(payload.get('expectancy_pct', 0.0)):+.2f}% | "
                    f"profit_factor={float(payload.get('profit_factor', 0.0)):.2f} | "
                    f"trades={int(payload.get('trade_count', 0))})"
                )

    if shadow_benchmark_watch and shadow_benchmark_watch.get("status") in {"ready", "baseline_confirmed"}:
        baseline = shadow_benchmark_watch.get("baseline") or {}
        watch = shadow_benchmark_watch.get("watch") or {}
        lines.extend(["", "## Shadow Benchmark Watch", ""])
        lines.append(f"- Focus Symbol: {shadow_benchmark_watch.get('focus_symbol', 'n/a')}")
        if shadow_benchmark_watch.get("selection_source"):
            lines.append(f"- Selection Source: {shadow_benchmark_watch.get('selection_source')}")
        lines.append(
            f"- Live Baseline: {baseline.get('candidate_id', 'n/a')} "
            f"(expectancy={float(baseline.get('expectancy_pct', 0.0)):+.2f}% | "
            f"profit_factor={float(baseline.get('profit_factor', 0.0)):.2f} | "
            f"trades={int(baseline.get('trade_count', 0))})"
        )
        baseline_cost_note = _benchmark_cost_note(baseline)
        if baseline_cost_note:
            lines.append(f"  cost={baseline_cost_note}")
        if shadow_benchmark_watch.get("status") == "ready":
            lines.append(
                f"- Shadow Candidate: {watch.get('candidate_id', 'n/a')} "
                f"(expectancy={float(watch.get('expectancy_pct', 0.0)):+.2f}% | "
                f"profit_factor={float(watch.get('profit_factor', 0.0)):.2f} | "
                f"trades={int(watch.get('trade_count', 0))})"
            )
            watch_cost_note = _benchmark_cost_note(watch)
            if watch_cost_note:
                lines.append(f"  cost={watch_cost_note}")
            lines.append(
                f"- Delta: expectancy {float(shadow_benchmark_watch.get('expectancy_delta_pct', 0.0)):+.2f}% | "
                f"profit factor {float(shadow_benchmark_watch.get('profit_factor_delta', 0.0)):+.2f} | "
                f"cumulative {float(shadow_benchmark_watch.get('cumulative_return_delta_pct', 0.0)):+.2f}% | "
                f"trades {int(shadow_benchmark_watch.get('trade_count_delta', 0)):+d}"
            )
            lines.append(
                f"- Promotion Streak: {int(shadow_benchmark_watch.get('promotion_streak', 0))} "
                f"(qualified now={bool(shadow_benchmark_watch.get('current_snapshot_qualified', False))})"
            )
        lines.append(f"- Verdict: {shadow_benchmark_watch.get('verdict', 'n/a')}")
        if shadow_benchmark_watch.get("summary"):
            lines.append(f"- Summary: {shadow_benchmark_watch.get('summary')}")
        if shadow_benchmark_watch.get("next_step"):
            lines.append(f"- Next Step: {shadow_benchmark_watch.get('next_step')}")

    if strategy_research_latest:
        recommendation = strategy_research_latest.get("recommendation") or {}
        aggregate_ranking = list(strategy_research_latest.get("aggregate_ranking") or [])
        runs = list(strategy_research_latest.get("runs") or [])
        lines.extend(["", "## Idle-Time Strategy Research", ""])
        lines.append(f"- Generated At: {strategy_research_latest.get('generated_at', 'n/a')}")
        lines.append(f"- Focus Symbol: {strategy_research_latest.get('focus_symbol', 'n/a')}")
        validation_symbols = strategy_research_latest.get("validation_symbols") or []
        if validation_symbols:
            lines.append(f"- Validation Symbols: {', '.join(str(item) for item in validation_symbols)}")
        limits = strategy_research_latest.get("limits") or []
        if limits:
            lines.append(f"- Lookback Windows: {', '.join(str(int(item)) for item in limits)} candles")
        lines.append(
            f"- Recommendation: {recommendation.get('candidate_id', 'n/a')} / {recommendation.get('verdict', 'n/a')}"
        )
        if recommendation.get("rationale"):
            lines.append(f"- Rationale: {recommendation.get('rationale')}")
        if aggregate_ranking:
            top = aggregate_ranking[0]
            lines.append(
                f"- Aggregate Leader: {top.get('candidate_id', 'n/a')} "
                f"(focus positive windows={int(top.get('focus_positive_windows', 0))}/{int(top.get('focus_window_count', 0))} | "
                f"avg focus expectancy={float(top.get('avg_focus_expectancy_pct', 0.0)):+.2f}% | "
                f"avg focus PF={float(top.get('avg_focus_profit_factor', 0.0)):.2f} | "
                f"validation pass={'yes' if bool(top.get('validation_guard_pass', False)) else 'no'})"
            )
        if runs:
            lines.append("- Per-Window Leaders:")
            for run in runs[:6]:
                top_candidate = run.get("top_candidate") or {}
                lines.append(
                    f"  - {run.get('symbol', 'n/a')} / {int(run.get('limit', 0) or 0)} candles: "
                    f"{top_candidate.get('candidate_id', 'n/a')} "
                    f"(expectancy={float(top_candidate.get('expectancy_pct', 0.0)):+.2f}% | "
                    f"pf={float(top_candidate.get('profit_factor', 0.0)):.2f} | "
                    f"trades={int(top_candidate.get('trade_count', 0) or 0)})"
                )

    if trade_review.get("episodes"):
        lines.extend(["", "## Trade Review", ""])
        lines.append(
            f"- Position Episodes: long={int(trade_review.get('long_episodes', 0))} | "
            f"short={int(trade_review.get('short_episodes', 0))} | "
            f"wins={int(trade_review.get('closed_winners', 0))} | "
            f"losses={int(trade_review.get('closed_losers', 0))} | "
            f"open={int(trade_review.get('open_episodes', 0))}"
        )
        for item in trade_review.get("episodes", [])[:8]:
            lines.append(
                f"- {item.get('symbol', 'n/a')} {item.get('direction', 'n/a')} "
                f"opened {item.get('opened_at', 'n/a')} | entries={int(item.get('entries', 0))} | "
                f"avg_entry={float(item.get('avg_entry_price', 0.0)):.4f} | "
                f"close={float(item.get('close_price', 0.0)):.4f} | "
                f"edge={float(item.get('estimated_edge_pct', 0.0)):+.2f}% | "
                f"status={item.get('status', 'n/a')} | "
                f"entry_source={item.get('entry_source', 'unknown')}"
                f"{' | carry_in=yes' if item.get('carry_in') else ''}"
            )
            if item.get("close_reason"):
                lines.append(f"  close_reason: {item.get('close_reason')}")

    if policy_exit_diagnostics:
        lines.extend(["", "## Policy Exit Diagnostics", ""])
        lines.append(f"- Summary: {policy_exit_diagnostics.get('summary', 'n/a')}")
        lines.append(f"- Policy decisions: {int(policy_exit_diagnostics.get('policy_decision_count', 0))}")
        lines.append(f"- Accepted policy exits: {int(policy_exit_diagnostics.get('accepted_policy_exit_count', 0))}")
        lines.append(f"- Stagnation exits: {int(policy_exit_diagnostics.get('stagnation_exit_count', 0))}")
        lines.append(f"- Max-hold exits: {int(policy_exit_diagnostics.get('max_hold_exit_count', 0))}")
        lines.append(f"- End-of-day exits: {int(policy_exit_diagnostics.get('end_of_day_exit_count', 0))}")

    if loss_attribution:
        lines.extend(["", "## Loss Attribution", ""])
        lines.append(f"- Primary Driver: {loss_attribution.get('primary_driver', 'n/a')}")
        lines.append(
            f"- Realized After Fees: {float(loss_attribution.get('realized_after_fees_usdt', 0.0)):+.2f} USDT"
        )
        lines.append(
            f"- Live Trade Expectancy: {float(loss_attribution.get('live_trade_expectancy_pct', 0.0)):+.2f}% "
            f"(win rate {float(loss_attribution.get('live_trade_win_rate_pct', 0.0)):.1f}% | "
            f"avg win {float(loss_attribution.get('avg_win_edge_pct', 0.0)):+.2f}% | "
            f"avg loss {float(loss_attribution.get('avg_loss_edge_pct', 0.0)):+.2f}%)"
        )
        live_pf = loss_attribution.get("live_profit_factor")
        if loss_attribution.get("live_profit_factor_infinite"):
            lines.append("- Live Profit Factor: inf (no closed losing episodes in this window)")
        elif live_pf is not None:
            lines.append(f"- Live Profit Factor: {float(live_pf):.2f}")
        else:
            lines.append("- Live Profit Factor: n/a")
        cost_ratio = loss_attribution.get("cost_impact_ratio")
        if cost_ratio is not None:
            lines.append(
                f"- Cost Impact Ratio: {float(cost_ratio):.2f} "
                f"({loss_attribution.get('cost_impact_ratio_basis', 'fees over realized pnl')})"
            )
        else:
            lines.append(
                f"- Cost Impact Ratio: n/a "
                f"({loss_attribution.get('cost_impact_ratio_basis', 'realized pnl too small to assess')})"
            )
        accepted = loss_attribution.get("accepted_source_counts") or {}
        if accepted:
            lines.append(
                "- Accepted by Source: "
                + " | ".join(f"{k}={int(v)}" for k, v in accepted.items())
            )
        lines.append(
            f"- Closed Episodes: {int(loss_attribution.get('closed_episode_count', 0))} "
            f"(wins={int(loss_attribution.get('winning_episode_count', 0))} | "
            f"losses={int(loss_attribution.get('losing_episode_count', 0))} | "
            f"flat={int(loss_attribution.get('flat_episode_count', 0))})"
        )
        losing_sources = loss_attribution.get("losing_episode_source_counts") or {}
        if losing_sources:
            lines.append(
                "- Losing Episodes by Source: "
                + " | ".join(f"{k}={int(v)}" for k, v in losing_sources.items())
            )
        losing_dirs = loss_attribution.get("losing_episode_direction_counts") or {}
        if losing_dirs:
            lines.append(
                "- Losing Episodes by Direction: "
                + " | ".join(f"{k}={int(v)}" for k, v in losing_dirs.items())
            )
        avg_loss_source = loss_attribution.get("avg_loss_edge_by_source_pct") or {}
        if avg_loss_source:
            lines.append(
                "- Avg Losing Edge by Source: "
                + " | ".join(f"{k}={float(v):+.2f}%" for k, v in avg_loss_source.items())
            )
        benchmark_payload = loss_attribution.get("focus_symbol_benchmark") or {}
        if benchmark_payload.get("candidate_id"):
            lines.append(
                f"- Benchmark Check ({loss_attribution.get('focus_symbol', 'n/a')}): "
                f"{benchmark_payload.get('candidate_id')} "
                f"(expectancy={float(benchmark_payload.get('expectancy_pct', 0.0)):+.2f}% | "
                f"profit_factor={float(benchmark_payload.get('profit_factor', 0.0)):.2f} | "
                f"trades={int(benchmark_payload.get('trade_count', 0))})"
            )
        worst_episode = loss_attribution.get("worst_episode") or {}
        if worst_episode:
            lines.append(
                f"- Worst Episode: {worst_episode.get('symbol', 'n/a')} {worst_episode.get('direction', 'n/a')} "
                f"source={worst_episode.get('entry_source', 'unknown')} "
                f"edge={float(worst_episode.get('estimated_edge_pct', 0.0)):+.2f}%"
            )
        for item in loss_attribution.get("observations", [])[:5]:
            lines.append(f"- Observation: {item}")

    po3_phase_performance = summary.get("po3_phase_performance") or {}
    po3_rows = po3_phase_performance.get("rows") or []
    if po3_rows:
        lines.extend(["", "## PO3 Phase Performance", ""])
        for row in po3_rows:
            phase = str(row.get("phase", "unknown") or "unknown")
            expectancy_label = (
                f"{float(row.get('expectancy_after_fees', row.get('expectancy_after_fees_pct', 0.0))):+.2f}%"
                if row.get("expectancy_after_fees_pct") is not None
                else "n/a"
            )
            win_rate_label = (
                f"{float(row.get('win_rate_pct', 0.0)):.1f}%"
                if row.get("win_rate_pct") is not None
                else "n/a"
            )
            selected_expectancy_label = (
                f"{float(row.get('selected_expectancy_pct', 0.0)):+.2f}%"
                if row.get("selected_expectancy_pct") is not None
                else "n/a"
            )
            lines.append(
                f"- {phase}: proposals={int(row.get('proposal_count', 0) or 0)} | "
                f"approved={int(row.get('approved_count', 0) or 0)} | "
                f"executed={int(row.get('executed_count', 0) or 0)} | "
                f"closed={int(row.get('closed_count', 0) or 0)} | "
                f"win_rate={win_rate_label} | "
                f"expectancy_after_fees={expectancy_label} | "
                f"selected_replay_expectancy={selected_expectancy_label}"
            )
        if po3_phase_performance.get("note"):
            lines.append(f"- Attribution Note: {po3_phase_performance.get('note')}")

    if control_impact:
        lines.extend(["", "## Control Impact", ""])
        changed_controls = control_impact.get("changed_controls") or {}
        if changed_controls:
            lines.append(
                "- Changed Controls: "
                + " | ".join(
                    f"{key}={payload.get('previous', 'n/a')} -> {payload.get('current', 'n/a')}"
                    for key, payload in changed_controls.items()
                    if isinstance(payload, dict)
                )
            )
        experiment = control_impact.get("experiment") or {}
        if experiment:
            lines.append(
                f"- Active Experiment: {experiment.get('experiment_id', 'n/a')} "
                f"(ttl_windows={int(experiment.get('ttl_windows', 0) or 0)} | "
                f"trigger={experiment.get('trigger', 'n/a')})"
            )
            success_metrics = experiment.get("success_metrics") or []
            if success_metrics:
                lines.append(f"- Success Metrics: {', '.join(str(item) for item in success_metrics)}")
            lines.append(
                f"- Sample Guard: {'on' if bool(experiment.get('sample_guard_active')) else 'off'}"
            )
        lines.append(
            f"- Accepted Rate: {float(control_impact.get('accepted_rate_pct', 0.0)):.2f}% "
            f"(delta {float(control_impact.get('accepted_rate_delta_pct', 0.0)):+.2f}pp)"
        )
        lines.append(
            f"- Hold Ratio: {float(control_impact.get('hold_ratio_pct', 0.0)):.2f}% "
            f"(delta {float(control_impact.get('hold_ratio_delta_pct', 0.0)):+.2f}pp)"
        )
        if control_impact.get("cost_impact_ratio") is not None:
            lines.append(
                f"- Cost Impact Ratio: {float(control_impact.get('cost_impact_ratio', 0.0)):.2f} "
                f"(delta {float(control_impact.get('cost_impact_ratio_delta', 0.0) or 0.0):+.2f})"
            )
        lines.append(
            f"- Accepted Policy Exits: {int(control_impact.get('accepted_policy_exit_count', 0) or 0)} "
            f"(delta {int(control_impact.get('accepted_policy_exit_delta', 0) or 0):+d})"
        )
        avg_hold_bars = control_impact.get("avg_hold_bars")
        if avg_hold_bars is not None:
            lines.append(
                f"- Avg Hold Bars (closed episodes): {float(avg_hold_bars):.2f} "
                f"(delta {float(control_impact.get('avg_hold_bars_delta', 0.0) or 0.0):+.2f})"
            )

    if agent_trace_archive or ground_truth_artifact or oracle_postmortem_artifact:
        lines.extend(["", "## Research Artifacts", ""])
        if agent_trace_archive.get("directory"):
            lines.append(f"- Agent Trace Archive: {agent_trace_archive.get('directory')}")
        if ground_truth_artifact.get("json_path") or ground_truth_artifact.get("md_path"):
            lines.append(
                f"- Ground Truth: {ground_truth_artifact.get('json_path', 'n/a')} | "
                f"{ground_truth_artifact.get('md_path', 'n/a')}"
            )
        if oracle_postmortem_artifact.get("json_path") or oracle_postmortem_artifact.get("md_path"):
            lines.append(
                f"- Oracle Postmortem: {oracle_postmortem_artifact.get('json_path', 'n/a')} | "
                f"{oracle_postmortem_artifact.get('md_path', 'n/a')}"
            )

    if daily_strategy_review:
        lines.extend(["", "## Strategy Review", ""])
        if daily_strategy_review.get("strategist_review"):
            lines.append(f"- Strategist View: {daily_strategy_review.get('strategist_review')}")
        if daily_strategy_review.get("risk_review"):
            lines.append(f"- Risk View: {daily_strategy_review.get('risk_review')}")
        if daily_strategy_review.get("benchmark_review"):
            lines.append(f"- Benchmark View: {daily_strategy_review.get('benchmark_review')}")
        if daily_strategy_review.get("execution_review"):
            lines.append(f"- Execution View: {daily_strategy_review.get('execution_review')}")
        if daily_strategy_review.get("consensus_summary"):
            lines.append(f"- Consensus: {daily_strategy_review.get('consensus_summary')}")
        for item in daily_strategy_review.get("action_items", [])[:5]:
            lines.append(f"- Action Item: {item}")

    if external_ai_review and external_ai_review.get("status") not in {"disabled", ""}:
        lines.extend(["", "## External AI Review", ""])
        if external_ai_review.get("provider") or external_ai_review.get("model"):
            lines.append(
                f"- Reviewer: {external_ai_review.get('provider', 'n/a')} / {external_ai_review.get('model', 'n/a')}"
            )
        if external_ai_review.get("status"):
            lines.append(f"- Status: {external_ai_review.get('status')}")
        if external_ai_review.get("summary"):
            lines.append(f"- Summary: {external_ai_review.get('summary')}")
        if external_ai_review.get("verdict"):
            lines.append(f"- Verdict: {external_ai_review.get('verdict')}")
        for item in external_ai_review.get("strengths", [])[:4]:
            lines.append(f"- Strength: {item}")
        for item in external_ai_review.get("concerns", [])[:4]:
            lines.append(f"- Concern: {item}")
        for item in external_ai_review.get("action_items", [])[:4]:
            lines.append(f"- External Action Item: {item}")

    if mentor_review and mentor_review.get("status") not in {"disabled", ""}:
        lines.extend(["", "## Mentor Review", ""])
        role_summaries = mentor_review.get("role_summaries") or {}
        for role in ("strategist", "risk_supervisor", "executor", "strategy_reflector"):
            summaries = role_summaries.get(role) or []
            first_ok = next((item for item in summaries if item.get("status") == "ok" and item.get("summary")), None)
            if first_ok:
                lines.append(f"- {role}: {first_ok.get('summary')}")
                for finding in list(first_ok.get("findings") or [])[:3]:
                    lines.append(f"  finding: {finding}")

        consensus = mentor_review.get("consensus") or {}
        safe_patch = (consensus.get("safe_patch") or {}).get("controls_patch") or {}
        conflict_patch = (consensus.get("conflict_patch") or {}).get("controls_patch") or {}
        lines.extend(["", "## Mentor Consensus", ""])
        lines.append(f"- Safe Controls: {json.dumps(safe_patch, ensure_ascii=False, sort_keys=True)}")
        lines.append(f"- Conflict Controls: {json.dumps(conflict_patch, ensure_ascii=False, sort_keys=True)}")

        gate = mentor_review.get("gate") or {}
        lines.extend(["", "## Shadow Gate", ""])
        lines.append(f"- Status: {gate.get('status', 'n/a')} | Candidate: {gate.get('candidate_id', 'n/a')}")
        for reason in list(gate.get("reasons") or [])[:5]:
            lines.append(f"- Reason: {reason}")

        promotion = mentor_review.get("promotion") or {}
        lines.extend(["", "## Promotion", ""])
        lines.append(f"- Status: {promotion.get('status', 'n/a')}")
        lines.append(f"- Promoted Keys: {', '.join(promotion.get('promoted_keys') or []) or 'none'}")

    if symbol_postmortem:
        if market_path_review:
            lines.extend(["", "## Market Path Review", ""])
            lines.append(f"- Focus Symbol: {market_path_review.get('symbol', 'n/a')}")
            if str(market_path_review.get("coverage_status", "")).strip():
                lines.append(
                    f"- Coverage: {market_path_review.get('coverage_status', 'n/a')} | "
                    f"samples={int(market_path_review.get('sample_count', 0) or 0)} | "
                    f"span={float(market_path_review.get('sample_span_hours', 0.0) or 0.0):.2f}h / "
                    f"{float(market_path_review.get('window_hours', 0.0) or 0.0):.2f}h"
                )
                if str(market_path_review.get("coverage_note", "")).strip():
                    lines.append(f"- Coverage Note: {market_path_review.get('coverage_note', '')}")
            lines.append(f"- Summary: {market_path_review.get('summary', '')}")
            lines.append(
                f"- Sampled Path: start {market_path_review.get('first_timestamp_local', 'n/a')} @ "
                f"{float(market_path_review.get('first_price', 0.0)):.4f} | "
                f"high {market_path_review.get('high_timestamp_local', 'n/a')} @ {float(market_path_review.get('high_price', 0.0)):.4f} | "
                f"low {market_path_review.get('low_timestamp_local', 'n/a')} @ {float(market_path_review.get('low_price', 0.0)):.4f} | "
                f"end {market_path_review.get('last_timestamp_local', 'n/a')} @ {float(market_path_review.get('last_price', 0.0)):.4f}"
            )
            lines.append(
                f"- Largest Down Leg: {market_path_review.get('max_drawdown_start_local', 'n/a')} "
                f"{float(market_path_review.get('max_drawdown_start_price', 0.0)):.4f} -> "
                f"{market_path_review.get('max_drawdown_end_local', 'n/a')} {float(market_path_review.get('max_drawdown_end_price', 0.0)):.4f} "
                f"({float(market_path_review.get('max_drawdown_pct', 0.0)):+.2f}%)"
            )
            lines.append(
                f"- Largest Rebound: {market_path_review.get('max_rebound_start_local', 'n/a')} "
                f"{float(market_path_review.get('max_rebound_start_price', 0.0)):.4f} -> "
                f"{market_path_review.get('max_rebound_end_local', 'n/a')} {float(market_path_review.get('max_rebound_end_price', 0.0)):.4f} "
                f"({float(market_path_review.get('max_rebound_pct', 0.0)):+.2f}%)"
            )
            if market_path_review.get("max_drawdown_action_counts"):
                counts = market_path_review.get("max_drawdown_action_counts") or {}
                lines.append(
                    "- Actions During Down Leg: "
                    + " | ".join(f"{key}={int(value)}" for key, value in counts.items())
                )
            if market_path_review.get("max_rebound_action_counts"):
                counts = market_path_review.get("max_rebound_action_counts") or {}
                lines.append(
                    "- Actions During Rebound: "
                    + " | ".join(f"{key}={int(value)}" for key, value in counts.items())
                )
        lines.extend(["", "## Symbol Postmortem", ""])
        lines.append(f"- Focus Symbol: {symbol_postmortem.get('symbol', 'n/a')}")
        lines.append(f"- Summary: {symbol_postmortem.get('summary', '')}")
        for item in symbol_postmortem.get("improvement_directions", [])[:4]:
            lines.append(f"- Improvement: {item}")

    if latest:
        lines.extend(
            [
                "",
                "## Latest Decision",
                "",
                f"- Selected Symbol: {latest.get('selected_symbol', 'n/a')}",
                f"- Conclusion: {_summary_line(latest)}",
                f"- Signal: {latest['idea']['action']} (score={float(latest['idea']['score']):.2f})",
                f"- Decision Source: {latest.get('decision_source', 'unknown')}",
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
        market_structure = latest.get("market_structure") or {}
        if market_structure:
            lines.append(
                "- Market Structure: "
                f"PO3={market_structure.get('po3_phase_hint', 'unknown')} | "
                f"POC {float(market_structure.get('poc_distance_bps', 0.0)):+.1f}bps | "
                f"VAH {float(market_structure.get('value_area_high_distance_bps', 0.0)):+.1f}bps | "
                f"VAL {float(market_structure.get('value_area_low_distance_bps', 0.0)):+.1f}bps"
            )
            lines.append(
                "- FVG Context: "
                f"bullish {float(market_structure.get('nearest_bullish_fvg_distance_bps', 0.0)):+.1f}bps | "
                f"bearish {float(market_structure.get('nearest_bearish_fvg_distance_bps', 0.0)):+.1f}bps | "
                f"fill {float(market_structure.get('fvg_fill_ratio', 0.0)):.2f}"
            )
        if latest.get("idea", {}).get("rationale"):
            lines.append(f"- Why This Decision: {latest['idea']['rationale']}")
        strategy_memory = summary.get("strategy_memory_current") or latest.get("strategy_memory") or {}
        controls = strategy_memory.get("controls") or {}
        if controls:
            lines.append(f"- Learning Controls: {json.dumps(controls, ensure_ascii=False)}")
        debate = latest.get("debate") or {}
        if debate.get("risk_feedback"):
            lines.append(f"- Debate: risk raised `{debate['risk_feedback']}` before final decision")
        if debate.get("fallback_guard_reason"):
            lines.append(f"- Guardrail: {debate['fallback_guard_reason']}")
        if debate.get("memory_guard_reason"):
            lines.append(f"- Memory Guard: {debate['memory_guard_reason']}")
        account = latest.get("account")
        if account:
            if account.get("market_type") == "perp":
                opened_at_local = str(account.get("opened_at_local") or (latest.get("position_context") or {}).get("opened_at_local") or "").strip() or "n/a"
                protection_profile = latest.get("protection_profile") or {}
                lines.extend(
                    [
                        (
                            f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                            f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT"
                        ),
                        (
                            f"- Current Position: {account.get('position_side', 'flat')} "
                            f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']}"
                        ),
                        f"- Position Opened: {opened_at_local}",
                        (
                            f"- Entry / Mark: {float(account.get('entry_price', 0.0)):.4f} -> "
                            f"{float(account.get('mark_price', latest.get('last_price', 0.0))):.4f}"
                        ),
                        (
                            f"- Take Profit / Stop Loss: "
                            f"{float(account.get('take_profit_price', 0.0)):.4f} / "
                            f"{float(account.get('stop_loss_price', 0.0)):.4f}"
                        ),
                        (
                            f"- Protection Logic: {str(protection_profile.get('regime', 'normal'))} | "
                            f"ATR {float(protection_profile.get('atr_pct', 0.0)):.2f}% | "
                            f"range {float(protection_profile.get('range_pct', 0.0)):.2f}% | "
                            f"efficiency {float(protection_profile.get('efficiency', 0.0)):.2f}"
                        ),
                        (
                            f"- Position Risk: UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT | "
                            f"Lev {float(account.get('leverage', 0.0)):.2f}x | "
                            f"Liq {float(account.get('liq_price', 0.0)):.4f} | "
                            f"Buffer {float(account.get('liquidation_buffer_pct', 0.0)):.2f}%"
                        ),
                    ]
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
    return active_report_date_label()


def _build_ground_truth_payload(summary: dict) -> dict[str, Any]:
    financial = summary.get("financial_snapshot") or {}
    market_path = summary.get("market_path_review") or {}
    loss = summary.get("loss_attribution") or {}
    trade_review = summary.get("trade_review") or {}
    focus_benchmark = loss.get("focus_symbol_benchmark") or summary.get("benchmark_watch_candidate_current") or {}
    if not isinstance(focus_benchmark, dict):
        focus_benchmark = {}
    strategy_research_latest = summary.get("strategy_research_latest") or {}
    recommendation = strategy_research_latest.get("recommendation") or {}
    if not isinstance(recommendation, dict):
        recommendation = {}
    return {
        "date_label": summary.get("date_label", ""),
        "window_start": summary.get("window_start", ""),
        "window_end": summary.get("window_end", ""),
        "focus_symbol": str(market_path.get("symbol", "") or summary.get("focus_symbol", "") or ""),
        "market_path_review": market_path,
        "financial_snapshot": {
            "daily_pnl_usdt": float(financial.get("daily_pnl_usdt", 0.0) or 0.0),
            "daily_pnl_pct": float(financial.get("daily_pnl_pct", 0.0) or 0.0),
            "realized_pnl_usdt": float(financial.get("realized_pnl_usdt", 0.0) or 0.0),
            "unrealized_pnl_usdt": float(financial.get("unrealized_pnl_usdt", 0.0) or 0.0),
            "daily_fees_usdt": float(financial.get("daily_fees_usdt", 0.0) or 0.0),
        },
        "live_actions": {
            "total_decisions": int(summary.get("total", 0) or 0),
            "accepted_orders": int(summary.get("accepted_orders", 0) or 0),
            "hold_count": int(summary.get("holds", 0) or 0),
            "action_counts": summary.get("action_counts") or {},
            "executed_trade_timeline": summary.get("executed_trade_timeline") or [],
            "trade_review": trade_review,
        },
        "benchmark_context": {
            "focus_symbol_benchmark": focus_benchmark,
            "strategy_research_recommendation": recommendation,
            "shadow_benchmark_watch": summary.get("shadow_benchmark_watch") or {},
        },
        "loss_attribution": loss,
    }


def _build_oracle_postmortem_payload(summary: dict) -> dict[str, Any]:
    market_path = summary.get("market_path_review") or {}
    financial = summary.get("financial_snapshot") or {}
    loss = summary.get("loss_attribution") or {}
    focus_benchmark = loss.get("focus_symbol_benchmark") or summary.get("benchmark_watch_candidate_current") or {}
    if not isinstance(focus_benchmark, dict):
        focus_benchmark = {}
    strategy_research_latest = summary.get("strategy_research_latest") or {}
    recommendation = strategy_research_latest.get("recommendation") or {}
    if not isinstance(recommendation, dict):
        recommendation = {}
    max_drawdown_pct = float(market_path.get("max_drawdown_pct", 0.0) or 0.0)
    max_rebound_pct = float(market_path.get("max_rebound_pct", 0.0) or 0.0)
    down_actions = market_path.get("max_drawdown_action_counts") or {}
    rebound_actions = market_path.get("max_rebound_action_counts") or {}
    accepted_orders = int(summary.get("accepted_orders", 0) or 0)
    hold_count = int(summary.get("holds", 0) or 0)
    total = int(summary.get("total", 0) or 0)
    hold_ratio = (hold_count / total) if total > 0 else 0.0
    coverage_status = str(market_path.get("coverage_status", "") or "").strip()

    root_causes: list[str] = []
    if int(loss.get("carry_in_closed_count", 0) or 0) > 0 and float(financial.get("daily_pnl_usdt", 0.0) or 0.0) < 0:
        root_causes.append("carry_in_drag")
    if abs(max_rebound_pct) >= 1.0 and int(rebound_actions.get("hold", 0) or 0) >= max(int(rebound_actions.get("buy", 0) or 0), 1):
        root_causes.append("missed_rebound_participation")
    if abs(max_drawdown_pct) >= 1.0 and int(down_actions.get("hold", 0) or 0) >= max(int(down_actions.get("sell", 0) or 0), 1):
        root_causes.append("missed_down_leg_participation")
    if accepted_orders == 0 and hold_ratio >= 0.85:
        root_causes.append("under_participation")
    if float(financial.get("daily_fees_usdt", 0.0) or 0.0) > max(float(financial.get("realized_pnl_usdt", 0.0) or 0.0), 0.0):
        root_causes.append("fee_drag")
    if coverage_status and coverage_status != "ok":
        root_causes.append("stale_market_path_evidence")

    best_candidate = focus_benchmark if str(focus_benchmark.get("candidate_id", "") or "").strip() else recommendation
    hindsight_method = "observe_only"
    if str(best_candidate.get("candidate_id", "") or "").strip():
        hindsight_method = str(best_candidate.get("candidate_id") or "")

    rationale_parts = []
    if root_causes:
        rationale_parts.append(f"root causes: {', '.join(root_causes)}")
    if hindsight_method != "observe_only":
        rationale_parts.append(f"best hindsight candidate: {hindsight_method}")
    if not rationale_parts:
        rationale_parts.append("no single dominant failure pattern detected")

    return {
        "date_label": summary.get("date_label", ""),
        "focus_symbol": str(market_path.get("symbol", "") or summary.get("focus_symbol", "") or ""),
        "best_hindsight_candidate_id": hindsight_method,
        "best_hindsight_expectancy_pct": float(best_candidate.get("expectancy_pct", 0.0) or 0.0),
        "best_hindsight_profit_factor": float(best_candidate.get("profit_factor", 0.0) or 0.0),
        "root_cause_tags": root_causes,
        "live_gap": {
            "accepted_orders": accepted_orders,
            "hold_ratio": round(hold_ratio, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "max_rebound_pct": round(max_rebound_pct, 4),
            "market_data_coverage_status": coverage_status or "n/a",
            "market_data_coverage_ratio": round(float(market_path.get("coverage_ratio", 0.0) or 0.0), 4),
        },
        "rationale": "; ".join(rationale_parts),
        "suggested_experiment": {
            "candidate_id": hindsight_method,
            "success_metrics": ["live_trade_expectancy_pct", "live_profit_factor", "cost_impact_ratio"],
            "window_type": "noon_to_noon",
        },
    }


def _render_ground_truth_markdown(payload: dict[str, Any]) -> str:
    market_path = payload.get("market_path_review") or {}
    live_actions = payload.get("live_actions") or {}
    benchmark_context = payload.get("benchmark_context") or {}
    focus_benchmark = benchmark_context.get("focus_symbol_benchmark") or {}
    recommendation = benchmark_context.get("strategy_research_recommendation") or {}
    lines = [
        f"# Ground Truth: {payload.get('date_label', 'n/a')}",
        "",
        f"- Window: {payload.get('window_start', 'n/a')} -> {payload.get('window_end', 'n/a')}",
        f"- Focus Symbol: {payload.get('focus_symbol', 'n/a')}",
        f"- Market Summary: {market_path.get('summary', 'n/a')}",
        f"- Market Data Coverage: {market_path.get('coverage_status', 'n/a')} ({float(market_path.get('coverage_ratio', 0.0) or 0.0) * 100.0:.1f}% window span)",
        f"- Largest Down Leg: {float(market_path.get('max_drawdown_pct', 0.0) or 0.0):+.2f}%",
        f"- Largest Rebound: {float(market_path.get('max_rebound_pct', 0.0) or 0.0):+.2f}%",
        f"- Live Decisions: total={int(live_actions.get('total_decisions', 0) or 0)} | accepted={int(live_actions.get('accepted_orders', 0) or 0)} | hold={int(live_actions.get('hold_count', 0) or 0)}",
        f"- Focus Benchmark: {focus_benchmark.get('candidate_id', 'n/a')} (expectancy={float(focus_benchmark.get('expectancy_pct', 0.0) or 0.0):+.2f}% | pf={float(focus_benchmark.get('profit_factor', 0.0) or 0.0):.2f})",
        f"- Strategy Research Recommendation: {recommendation.get('candidate_id', 'n/a')} / {recommendation.get('verdict', 'n/a')}",
        "",
    ]
    return "\n".join(lines) + "\n"


def _render_oracle_postmortem_markdown(payload: dict[str, Any]) -> str:
    experiment = payload.get("suggested_experiment") or {}
    root_causes = payload.get("root_cause_tags") or []
    live_gap = payload.get("live_gap") or {}
    lines = [
        f"# Oracle Postmortem: {payload.get('date_label', 'n/a')}",
        "",
        f"- Focus Symbol: {payload.get('focus_symbol', 'n/a')}",
        f"- Best Hindsight Candidate: {payload.get('best_hindsight_candidate_id', 'n/a')} (expectancy={float(payload.get('best_hindsight_expectancy_pct', 0.0) or 0.0):+.2f}% | pf={float(payload.get('best_hindsight_profit_factor', 0.0) or 0.0):.2f})",
        f"- Root Causes: {', '.join(str(item) for item in root_causes) if root_causes else 'n/a'}",
        f"- Live Gap: accepted={int(live_gap.get('accepted_orders', 0) or 0)} | hold_ratio={float(live_gap.get('hold_ratio', 0.0) or 0.0):.2f} | drawdown={float(live_gap.get('max_drawdown_pct', 0.0) or 0.0):+.2f}% | rebound={float(live_gap.get('max_rebound_pct', 0.0) or 0.0):+.2f}%",
        f"- Market Data Coverage: {live_gap.get('market_data_coverage_status', 'n/a')} ({float(live_gap.get('market_data_coverage_ratio', 0.0) or 0.0) * 100.0:.1f}% window span)",
        f"- Rationale: {payload.get('rationale', 'n/a')}",
        f"- Suggested Experiment: {experiment.get('candidate_id', 'n/a')} | metrics={', '.join(str(item) for item in (experiment.get('success_metrics') or []))}",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_ground_truth_artifacts(path: Path, date_label: str, summary: dict[str, Any]) -> dict[str, str]:
    payload = _build_ground_truth_payload(summary)
    json_path = path / f"{date_label}.json"
    md_path = path / f"{date_label}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    md_path.write_text(_render_ground_truth_markdown(payload))
    return {"json_path": str(json_path), "md_path": str(md_path)}


def write_oracle_postmortem_artifacts(path: Path, date_label: str, summary: dict[str, Any]) -> dict[str, str]:
    payload = _build_oracle_postmortem_payload(summary)
    json_path = path / f"{date_label}.json"
    md_path = path / f"{date_label}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    md_path.write_text(_render_oracle_postmortem_markdown(payload))
    return {"json_path": str(json_path), "md_path": str(md_path)}
