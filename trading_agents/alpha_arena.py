from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AlphaArenaSignal:
    timestamp_ms: int
    symbol: str
    model: str
    action: str
    confidence: float
    commentary: str
    source_url: str = ""


@dataclass(frozen=True)
class AlphaArenaBacktestResult:
    sample_count: int
    trade_count: int
    win_rate: float
    avg_return_pct: float
    cumulative_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float
    profit_factor: float


_TIMEFRAME_TO_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_TIMEFRAME_TO_BYBIT = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}


def _coerce_timestamp_ms(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        numeric = int(text)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    for parser in (
        lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            dt = parser(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return None


def _normalize_symbol(value: object, default_symbol: str | None = None) -> str:
    symbol = str(value or default_symbol or "").strip().upper()
    if not symbol:
        return ""
    if "/" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def _normalize_action(value: object) -> str:
    action = str(value or "").strip().lower()
    mapping = {
        "long": "buy",
        "bullish": "buy",
        "buy": "buy",
        "open_long": "buy",
        "close_short": "buy",
        "short": "sell",
        "bearish": "sell",
        "sell": "sell",
        "open_short": "sell",
        "close_long": "sell",
        "flat": "hold",
        "hold": "hold",
        "neutral": "hold",
    }
    return mapping.get(action, "")


def _coerce_confidence(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0 and score <= 100.0:
        score = score / 100.0
    return max(0.0, min(score, 1.0))


def normalize_signal_record(
    record: dict,
    *,
    default_symbol: str | None = None,
    default_model: str | None = None,
    source_url: str = "",
) -> AlphaArenaSignal | None:
    timestamp_ms = _coerce_timestamp_ms(
        record.get("timestamp_ms")
        or record.get("timestamp")
        or record.get("time")
        or record.get("created_at")
        or record.get("createdAt")
    )
    symbol = _normalize_symbol(record.get("symbol") or record.get("market") or record.get("ticker"), default_symbol)
    action = _normalize_action(
        record.get("action")
        or record.get("side")
        or record.get("direction")
        or record.get("position_side")
        or record.get("bias")
    )
    if timestamp_ms is None or not symbol or action not in {"buy", "sell", "hold"}:
        return None
    model = str(record.get("model") or record.get("agent") or record.get("name") or default_model or "unknown").strip()
    commentary = str(
        record.get("commentary")
        or record.get("reasoning")
        or record.get("rationale")
        or record.get("notes")
        or ""
    ).strip()
    return AlphaArenaSignal(
        timestamp_ms=timestamp_ms,
        symbol=symbol,
        model=model,
        action=action,
        confidence=_coerce_confidence(record.get("confidence") or record.get("score")),
        commentary=commentary,
        source_url=source_url,
    )


def load_alpha_arena_signals(
    input_path: str,
    *,
    default_symbol: str | None = None,
    default_model: str | None = None,
    source_url: str = "",
) -> list[AlphaArenaSignal]:
    payload = json.loads(Path(input_path).read_text())
    if isinstance(payload, dict):
        if isinstance(payload.get("signals"), list):
            raw_records = payload["signals"]
        elif isinstance(payload.get("data"), list):
            raw_records = payload["data"]
        else:
            raw_records = [payload]
    elif isinstance(payload, list):
        raw_records = payload
    else:
        raw_records = []
    signals: list[AlphaArenaSignal] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        normalized = normalize_signal_record(
            item,
            default_symbol=default_symbol,
            default_model=default_model,
            source_url=source_url,
        )
        if normalized is not None:
            signals.append(normalized)
    signals.sort(key=lambda item: item.timestamp_ms)
    return signals


def write_normalized_signals(signals: list[AlphaArenaSignal], output_path: str) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(asdict(item), ensure_ascii=False) for item in signals]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return str(path)


def fetch_bybit_public_klines(symbol: str, timeframe: str, *, limit: int = 300) -> list[dict[str, float | int]]:
    interval = _TIMEFRAME_TO_BYBIT.get(timeframe)
    if interval is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    params = urlencode(
        {
            "category": "linear",
            "symbol": symbol.replace("/", ""),
            "interval": interval,
            "limit": min(max(int(limit), 1), 1000),
        }
    )
    request = Request(f"https://api.bybit.com/v5/market/kline?{params}", headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("retCode") not in (0, None):
        raise RuntimeError(f"Bybit public API error: {payload.get('retMsg', 'unknown error')}")
    rows = []
    for item in payload.get("result", {}).get("list", []):
        rows.append(
            {
                "timestamp_ms": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            }
        )
    rows.sort(key=lambda item: int(item["timestamp_ms"]))
    return rows


def _simulate_signal_return(
    closes: list[float],
    *,
    entry_index: int,
    action: str,
    hold_bars: int,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> float:
    entry_price = float(closes[entry_index])
    if entry_price <= 0:
        return 0.0
    direction_sign = 1.0 if action == "buy" else -1.0
    last_return = 0.0
    for offset in range(1, max(hold_bars, 1) + 1):
        next_index = entry_index + offset
        if next_index >= len(closes):
            break
        current_return = ((float(closes[next_index]) - entry_price) / entry_price) * direction_sign
        last_return = current_return
        if take_profit_pct > 0 and current_return >= take_profit_pct:
            return take_profit_pct
        if stop_loss_pct > 0 and current_return <= -stop_loss_pct:
            return -stop_loss_pct
    return last_return


def backtest_alpha_arena_signals(
    signals: list[AlphaArenaSignal],
    candles: list[dict[str, float | int]],
    *,
    hold_bars: int = 4,
    take_profit_pct: float = 0.009,
    stop_loss_pct: float = 0.0045,
) -> dict[str, AlphaArenaBacktestResult]:
    closes = [float(item["close"]) for item in candles]
    timestamps = [int(item["timestamp_ms"]) for item in candles]
    grouped: dict[str, list[float]] = {}
    for signal in signals:
        if signal.action == "hold":
            continue
        entry_index = next((idx for idx, ts in enumerate(timestamps) if ts >= signal.timestamp_ms), None)
        if entry_index is None or entry_index >= len(closes) - 1:
            continue
        realized = _simulate_signal_return(
            closes,
            entry_index=entry_index,
            action=signal.action,
            hold_bars=hold_bars,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
        )
        grouped.setdefault(signal.model, []).append(realized)
        grouped.setdefault("__overall__", []).append(realized)
    results: dict[str, AlphaArenaBacktestResult] = {}
    for model, returns in grouped.items():
        trade_count = len(returns)
        wins = [item for item in returns if item > 0]
        losses = [item for item in returns if item < 0]
        win_rate = sum(1 for item in returns if item > 0) / trade_count if trade_count else 0.0
        avg_return_pct = fmean(returns) * 100 if returns else 0.0
        cumulative_return_pct = sum(returns) * 100
        avg_win_pct = fmean(wins) * 100 if wins else 0.0
        avg_loss_pct = fmean(losses) * 100 if losses else 0.0
        expectancy_pct = (win_rate * avg_win_pct) + ((1 - win_rate) * avg_loss_pct) if trade_count else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        results[model] = AlphaArenaBacktestResult(
            sample_count=len(signals),
            trade_count=trade_count,
            win_rate=win_rate,
            avg_return_pct=avg_return_pct,
            cumulative_return_pct=cumulative_return_pct,
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            expectancy_pct=expectancy_pct,
            profit_factor=profit_factor,
        )
    return results


def save_backtest_report(results: dict[str, AlphaArenaBacktestResult], output_path: str) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {key: asdict(value) for key, value in results.items()}
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2))
    return str(path)
