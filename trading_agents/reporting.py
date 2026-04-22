from __future__ import annotations

import json
import re
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


def _build_symbol_postmortem(
    records: list[dict[str, Any]],
    *,
    focus_symbol: str = "",
    external_benchmarks: dict[str, Any] | None = None,
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
    first_price = prices[0] if prices else 0.0
    last_price = prices[-1] if prices else 0.0
    low_price = min(prices) if prices else 0.0
    high_price = max(prices) if prices else 0.0
    net_move_pct = (((last_price - first_price) / first_price) * 100.0) if first_price > 0 else 0.0
    intraday_range_pct = (((high_price - low_price) / first_price) * 100.0) if first_price > 0 else 0.0

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
    benchmark_payload = ((external_benchmarks or {}).get("top_by_symbol") or {}).get(chosen_symbol, {})
    benchmark_summary = ""
    if isinstance(benchmark_payload, dict) and benchmark_payload.get("candidate_id"):
        benchmark_summary = (
            f" 外部 benchmark 同標的目前以 {benchmark_payload.get('candidate_id')} 領先，"
            f"expectancy {float(benchmark_payload.get('expectancy_pct', 0.0)):+.2f}%。"
        )

    regime_hint = "directional down day" if net_move_pct <= -1.0 else "directional up day" if net_move_pct >= 1.0 else "range / mixed day"
    if net_move_pct <= -1.0 and sells <= max(1, holds // 4):
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
    if benchmark_payload and benchmark_payload.get("candidate_id") and benchmark_payload.get("candidate_id") != "donchian_adx_perp_v1":
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


def _build_trade_review(records: list[dict[str, Any]]) -> dict[str, Any]:
    accepted_records = [item for item in records if _result_status(item) == "accepted"]
    if not accepted_records:
        return {
            "episodes": [],
            "long_episodes": 0,
            "short_episodes": 0,
            "closed_winners": 0,
            "closed_losers": 0,
            "open_episodes": 0,
        }

    episodes: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}

    def close_episode(symbol: str, close_record: dict[str, Any]) -> None:
        episode = active.pop(symbol, None)
        if not episode:
            return
        order = _order_payload(close_record)
        close_price = _safe_float(order.get("price")) or _safe_float(close_record.get("last_price"))
        close_time = _accepted_trade_timestamp(close_record)
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
            }
        )
        episodes.append(episode)

    for item in accepted_records:
        symbol = str(item.get("selected_symbol", "")).strip()
        if not symbol:
            continue
        order = _order_payload(item)
        side = str(order.get("side", "")).lower()
        reduce_only = bool(order.get("reduce_only"))
        quantity = _safe_float(order.get("quantity")) or _safe_float(item.get("result", {}).get("submitted_qty"))
        price = _safe_float(order.get("price")) or _safe_float(item.get("last_price"))
        timestamp = _accepted_trade_timestamp(item)
        source = _decision_source(item)

        if reduce_only:
            close_episode(symbol, item)
            continue

        direction = "long" if side == "buy" else "short"
        existing = active.get(symbol)
        if existing and existing.get("direction") != direction:
            # Direction flipped without a clean reduce-only close; end the old episode at the new entry price.
            synthetic_close = {
                **item,
                "idea": {
                    "rationale": "position direction flipped without explicit reduce-only close",
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
            }
        )
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
    realized_after_fees = realized_pnl - fees

    primary_driver = ""
    if accepted_source_counts.get("fallback", 0) > max(accepted_source_counts.get("base_strategy", 0), 1):
        primary_driver = "fallback dominated accepted trades"
    elif losing_by_direction.get("long", 0) > losing_by_direction.get("short", 0):
        primary_driver = "long episodes drove most closed losses"
    elif losing_by_direction.get("short", 0) > losing_by_direction.get("long", 0):
        primary_driver = "short episodes drove most closed losses"
    elif fees > max(abs(realized_pnl), 0.01):
        primary_driver = "fees outweighed realized trading edge"
    else:
        primary_driver = "mixed execution drag across entry sources"

    observations: list[str] = []
    if accepted_source_counts.get("fallback", 0) > max(accepted_source_counts.get("base_strategy", 0), 1):
        observations.append("accepted trades were still fallback-heavy")
    if losing_by_direction.get("long", 0) > losing_by_direction.get("short", 0):
        observations.append("closed long episodes lost more often than shorts")
    if losing_by_direction.get("short", 0) > losing_by_direction.get("long", 0):
        observations.append("closed short episodes lost more often than longs")
    if fees > max(abs(realized_pnl), 0.01):
        observations.append("fees remained a meaningful drag versus realized PnL")
    if symbol_benchmark.get("candidate_id") and symbol_benchmark.get("candidate_id") != "donchian_adx_perp_v1":
        observations.append(
            f"{focus_symbol or 'focus symbol'} benchmark leader remained {symbol_benchmark.get('candidate_id')}"
        )
    if worst_episode:
        observations.append(
            f"worst episode was {worst_episode.get('symbol', 'n/a')} {worst_episode.get('direction', 'n/a')} "
            f"from {worst_episode.get('entry_source', 'unknown')} at {float(worst_episode.get('estimated_edge_pct', 0.0)):+.2f}%"
        )

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
        "losing_episode_source_counts": dict(losing_by_source.most_common()),
        "winning_episode_source_counts": dict(winning_by_source.most_common()),
        "open_episode_source_counts": dict(open_by_source.most_common()),
        "losing_episode_direction_counts": dict(losing_by_direction.most_common()),
        "winning_episode_direction_counts": dict(winning_by_direction.most_common()),
        "avg_loss_edge_by_source_pct": avg_loss_by_source,
        "avg_loss_edge_by_direction_pct": avg_loss_by_direction,
        "realized_after_fees_usdt": round(realized_after_fees, 4),
        "focus_symbol": focus_symbol,
        "focus_symbol_benchmark": symbol_benchmark,
        "worst_episode": worst_episode or {},
        "observations": observations[:5],
    }


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
    if account:
        if account.get("market_type") == "perp":
            account_line = (
                f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT | "
                f"position {account.get('position_side', 'flat')} "
                f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']} "
                f"@ {float(account.get('entry_price', 0.0)):.4f} | "
                f"UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT | "
                f"Lev {float(account.get('leverage', 0.0)):.2f}x | "
                f"Liq {float(account.get('liq_price', 0.0)):.4f} | "
                f"Buffer {float(account.get('liquidation_buffer_pct', 0.0)):.2f}% | "
                f"TP {float(account.get('take_profit_price', 0.0)):.4f} | "
                f"SL {float(account.get('stop_loss_price', 0.0)):.4f}"
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
    if protection_result or protection_targets:
        lines.extend(["", "## Protection", ""])
        if protection_targets:
            lines.append(
                f"- Targets: TP {float(protection_targets.get('take_profit', 0.0)):.4f} | "
                f"SL {float(protection_targets.get('stop_loss', 0.0)):.4f} | "
                f"Trailing {float(protection_targets.get('trailing_stop', 0.0)):.4f}"
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
                    "leverage": _safe_float(item.get("leverage")),
                    "liq_price": _safe_float(item.get("liq_price")),
                    "position_im_usdt": _safe_float(item.get("position_im_usdt")),
                    "position_mm_usdt": _safe_float(item.get("position_mm_usdt")),
                    "take_profit_price": _safe_float(item.get("take_profit_price")),
                    "stop_loss_price": _safe_float(item.get("stop_loss_price")),
                    "trailing_stop_distance": _safe_float(item.get("trailing_stop_distance")),
                    "liquidation_buffer_pct": _safe_float(item.get("liquidation_buffer_pct")),
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
    decision_source_counts: Counter[str] = Counter()
    accepted_source_counts: Counter[str] = Counter()
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
        "decision_source_counts": dict(decision_source_counts.most_common()),
        "accepted_source_counts": dict(accepted_source_counts.most_common()),
        "latest": latest,
    }


def load_daily_summary_data(trade_logs_dir: Path, date_label: str, runner_log_path: Path | None = None) -> dict[str, Any]:
    from trading_agents.config import load_settings
    from trading_agents.external_benchmarks import load_external_benchmark_summary
    from trading_agents.storage import build_storage_layout, mode_scoped_path

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    records = _filter_records_by_mode(_load_daily_records(trade_logs_dir, date_label), settings.trading_mode)
    all_records = _filter_records_by_mode(_load_all_records(trade_logs_dir), settings.trading_mode)
    runner_event_counts = _load_runner_event_counts(runner_log_path, date_label)
    summary = summarize_daily_records(records, runner_event_counts)
    summary["mode"] = settings.trading_mode
    summary["financial_snapshot"] = _build_financial_snapshot(
        records,
        all_records,
        initial_balance_usdt=settings.initial_balance_usdt,
        taker_fee_pct=settings.taker_fee_pct,
    )
    summary["equity_curve"] = load_equity_curve_summary(
        mode_scoped_path(storage.equity_curve_history_state, settings.trading_mode),
        mode_scoped_path(storage.equity_curve_svg, settings.trading_mode),
    )
    summary["external_benchmarks"] = load_external_benchmark_summary(storage.external_benchmark_state)
    summary["trade_review"] = _build_trade_review(records)
    focus_symbol = settings.observation_pool[0] if len(settings.observation_pool) == 1 else ""
    summary["symbol_postmortem"] = _build_symbol_postmortem(
        records,
        focus_symbol=focus_symbol,
        external_benchmarks=summary["external_benchmarks"],
    )
    summary["loss_attribution"] = _build_loss_attribution(
        records,
        trade_review=summary["trade_review"],
        financial_snapshot=summary["financial_snapshot"],
        external_benchmarks=summary["external_benchmarks"],
        focus_symbol=focus_symbol,
    )
    return summary


def build_daily_summary(trade_logs_dir: Path, date_label: str, runner_log_path: Path | None = None) -> str:
    summary = load_daily_summary_data(trade_logs_dir, date_label, runner_log_path)
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
    decision_source_counts = summary.get("decision_source_counts", {})
    accepted_source_counts = summary.get("accepted_source_counts", {})
    trade_review = summary.get("trade_review", {})
    top_traded_symbol = next(iter(executed_symbol_counts.items()), ("n/a", 0))
    long_proposals = int(summary.get("long_proposals", 0))
    short_proposals = int(summary.get("short_proposals", 0))
    long_accepted = int(summary.get("long_accepted", 0))
    short_accepted = int(summary.get("short_accepted", 0))
    external_benchmarks = summary.get("external_benchmarks", {})
    top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
    top_alpha_benchmark = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]
    symbol_postmortem = summary.get("symbol_postmortem") or {}
    loss_attribution = summary.get("loss_attribution") or {}

    lines = [f"# Daily Summary: {date_label}", ""]
    if summary_mode:
        lines.extend([f"- Mode: {summary_mode}", ""])
    lines.extend(
        [
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
    if holdings:
        lines.append("- Positions:")
        for item in holdings:
            if item.get("market_type") == "perp":
                lines.append(
                    f"  - {item['asset']} {item.get('position_side', 'flat')}: {float(item['quantity']):.6f} "
                    f"(Notional: {float(item['value_usdt']):.2f} USDT | Entry: {float(item.get('entry_price', 0.0)):.4f} | "
                    f"Mark: {float(item['price']):.4f} | Weight: {float(item['weight_pct']):.1f}% | "
                    f"UPnL: {float(item['unrealized_pnl_usdt']):+.2f} USDT / {float(item['unrealized_pnl_pct']):+.2f}% | "
                    f"Lev: {float(item.get('leverage', 0.0)):.2f}x | Liq buffer: {float(item.get('liquidation_buffer_pct', 0.0)):.2f}% | "
                    f"TP: {float(item.get('take_profit_price', 0.0)):.4f} | SL: {float(item.get('stop_loss_price', 0.0)):.4f})"
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

    if top_benchmark.get("candidate_id"):
        lines.extend(["", "## External Benchmarks", ""])
        lines.append(f"- Refreshed at: {external_benchmarks.get('generated_at', 'n/a')}")
        lines.append(f"- Live baseline strategy: {external_benchmarks.get('baseline_strategy_id', 'n/a')}")
        lines.append(
            f"- Top benchmark overall: {top_benchmark.get('candidate_id')} on {top_benchmark.get('symbol', 'n/a')} "
            f"(expectancy={float(top_benchmark.get('expectancy_pct', 0.0)):+.2f}% | "
            f"profit_factor={float(top_benchmark.get('profit_factor', 0.0)):.2f} | "
            f"trades={int(top_benchmark.get('trade_count', 0))})"
        )
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
            )
            if item.get("close_reason"):
                lines.append(f"  close_reason: {item.get('close_reason')}")

    if loss_attribution:
        lines.extend(["", "## Loss Attribution", ""])
        lines.append(f"- Primary Driver: {loss_attribution.get('primary_driver', 'n/a')}")
        lines.append(
            f"- Realized After Fees: {float(loss_attribution.get('realized_after_fees_usdt', 0.0)):+.2f} USDT"
        )
        accepted = loss_attribution.get("accepted_source_counts") or {}
        if accepted:
            lines.append(
                "- Accepted by Source: "
                + " | ".join(f"{k}={int(v)}" for k, v in accepted.items())
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

    if symbol_postmortem:
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
        strategy_memory = latest.get("strategy_memory") or {}
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
                account_line = (
                    f"- Account: equity {float(account.get('total_equity_usdt', account['free_usdt'])):.2f} USDT | "
                    f"available {float(account.get('available_balance_usdt', account['free_usdt'])):.2f} USDT | "
                    f"position {account.get('position_side', 'flat')} "
                    f"{float(account.get('base_asset', 0.0)):.6f} {account['base_symbol']} "
                    f"@ {float(account.get('entry_price', 0.0)):.4f} | "
                    f"UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT | "
                    f"Lev {float(account.get('leverage', 0.0)):.2f}x | "
                    f"Liq {float(account.get('liq_price', 0.0)):.4f} | "
                    f"Buffer {float(account.get('liquidation_buffer_pct', 0.0)):.2f}% | "
                    f"TP {float(account.get('take_profit_price', 0.0)):.4f} | "
                    f"SL {float(account.get('stop_loss_price', 0.0)):.4f}"
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
