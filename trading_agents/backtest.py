from __future__ import annotations

from statistics import fmean

from trading_agents.models import BacktestSnapshot, MarketSnapshot, SentimentSnapshot


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        return []
    smoothed: list[float] = [sum(values[:period])]
    for value in values[period:]:
        smoothed.append(smoothed[-1] - (smoothed[-1] / period) + value)
    return smoothed


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> dict[str, list[float]]:
    length = min(len(highs), len(lows), len(closes))
    if period <= 0 or length < (period * 2):
        return {"adx": [], "plus_di": [], "minus_di": [], "atr": []}

    tr_values: list[float] = []
    plus_dm_values: list[float] = []
    minus_dm_values: list[float] = []
    for index in range(1, length):
        high = float(highs[index])
        low = float(lows[index])
        prev_high = float(highs[index - 1])
        prev_low = float(lows[index - 1])
        prev_close = float(closes[index - 1])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr_values.append(tr)
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)

    atr_smoothed = _wilder_smooth(tr_values, period)
    plus_smoothed = _wilder_smooth(plus_dm_values, period)
    minus_smoothed = _wilder_smooth(minus_dm_values, period)
    if not atr_smoothed or not plus_smoothed or not minus_smoothed:
        return {"adx": [], "plus_di": [], "minus_di": [], "atr": []}

    plus_di: list[float] = []
    minus_di: list[float] = []
    dx_values: list[float] = []
    for atr_value, plus_value, minus_value in zip(atr_smoothed, plus_smoothed, minus_smoothed):
        if atr_value <= 0:
            plus_di.append(0.0)
            minus_di.append(0.0)
            dx_values.append(0.0)
            continue
        plus_di_value = (plus_value / atr_value) * 100.0
        minus_di_value = (minus_value / atr_value) * 100.0
        plus_di.append(plus_di_value)
        minus_di.append(minus_di_value)
        denominator = plus_di_value + minus_di_value
        dx_values.append((abs(plus_di_value - minus_di_value) / denominator) * 100.0 if denominator > 0 else 0.0)

    if len(dx_values) < period:
        return {"adx": [], "plus_di": plus_di, "minus_di": minus_di, "atr": [value / period for value in atr_smoothed]}

    adx: list[float] = [sum(dx_values[:period]) / period]
    for dx_value in dx_values[period:]:
        adx.append(((adx[-1] * (period - 1)) + dx_value) / period)

    atr = [value / period for value in atr_smoothed]
    return {
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "atr": atr,
    }


def donchian_adx_signal(
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    index: int,
    channel_period: int = 20,
    adx_period: int = 14,
    adx_threshold: float = 20.0,
    volume_ratio_threshold: float = 1.15,
) -> tuple[str, dict[str, float]]:
    if index < max(channel_period, adx_period * 2):
        return "hold", {}
    prior_highs = highs[index - channel_period:index]
    prior_lows = lows[index - channel_period:index]
    if not prior_highs or not prior_lows:
        return "hold", {}

    adx_state = compute_adx(highs[: index + 1], lows[: index + 1], closes[: index + 1], period=adx_period)
    if not adx_state["adx"]:
        return "hold", {}
    latest_adx = adx_state["adx"][-1]
    latest_plus = adx_state["plus_di"][-1] if adx_state["plus_di"] else 0.0
    latest_minus = adx_state["minus_di"][-1] if adx_state["minus_di"] else 0.0
    latest_atr = adx_state["atr"][-1] if adx_state["atr"] else 0.0
    recent_volume = fmean(volumes[max(0, index - 2): index + 1]) if volumes else 0.0
    baseline_volume = fmean(volumes[max(0, index - 19): index + 1]) if volumes else 0.0
    volume_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
    upper_breakout = max(prior_highs)
    lower_breakdown = min(prior_lows)
    close = closes[index]

    metrics = {
        "adx": latest_adx,
        "plus_di": latest_plus,
        "minus_di": latest_minus,
        "atr": latest_atr,
        "volume_ratio": volume_ratio,
        "upper_breakout": upper_breakout,
        "lower_breakdown": lower_breakdown,
    }
    if latest_adx < adx_threshold or volume_ratio < volume_ratio_threshold:
        return "hold", metrics
    if close > upper_breakout and latest_plus >= latest_minus:
        return "long", metrics
    if close < lower_breakdown and latest_minus >= latest_plus:
        return "short", metrics
    return "hold", metrics


def _simulate_intraday_trade(
    closes: list[float],
    *,
    entry_index: int,
    direction: str,
    max_hold_bars: int,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> float:
    entry_price = float(closes[entry_index])
    if entry_price <= 0:
        return 0.0
    direction_sign = 1.0 if direction == "long" else -1.0
    last_return = 0.0
    for offset in range(1, max(max_hold_bars, 1) + 1):
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


def build_backtest_snapshot(
    *,
    sample_count: int,
    returns: list[float],
    summary_prefix: str | None,
    empty_summary: str,
) -> BacktestSnapshot:
    if not returns:
        return BacktestSnapshot(
            sample_count=sample_count,
            trade_count=0,
            win_rate=0.0,
            avg_return_pct=0.0,
            cumulative_return_pct=0.0,
            summary=empty_summary,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            expectancy_pct=0.0,
            profit_factor=0.0,
        )

    trade_count = len(returns)
    wins = [item for item in returns if item > 0]
    losses = [item for item in returns if item < 0]
    win_rate = sum(1 for item in returns if item > 0) / trade_count
    avg_return_pct = fmean(returns) * 100
    cumulative_return_pct = sum(returns) * 100
    avg_win_pct = fmean(wins) * 100 if wins else 0.0
    avg_loss_pct = fmean(losses) * 100 if losses else 0.0
    expectancy_pct = (win_rate * avg_win_pct) + ((1 - win_rate) * avg_loss_pct)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    label = summary_prefix or "replay"
    summary = (
        f"{label} trades={trade_count}/{sample_count}; "
        f"win_rate={win_rate:.0%}; avg_return={avg_return_pct:+.2f}%; "
        f"expectancy={expectancy_pct:+.2f}%; pf={profit_factor:.2f}; "
        f"cumulative={cumulative_return_pct:+.2f}%"
    )
    return BacktestSnapshot(
        sample_count=sample_count,
        trade_count=trade_count,
        win_rate=win_rate,
        avg_return_pct=avg_return_pct,
        cumulative_return_pct=cumulative_return_pct,
        summary=summary,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        expectancy_pct=expectancy_pct,
        profit_factor=profit_factor,
    )


class BacktestAgent:
    name = "backtester"

    def evaluate(self, snapshot: MarketSnapshot, sentiment: SentimentSnapshot) -> BacktestSnapshot:
        closes = snapshot.closes
        highs = snapshot.highs or closes
        lows = snapshot.lows or closes
        volumes = snapshot.volumes
        if len(closes) < 35:
            return BacktestSnapshot(
                sample_count=0,
                trade_count=0,
                win_rate=0.0,
                avg_return_pct=0.0,
                cumulative_return_pct=0.0,
                summary="not enough candles for Donchian/ADX replay",
            )

        returns: list[float] = []
        start_index = 28
        for index in range(start_index, len(closes) - 6):
            direction, metrics = donchian_adx_signal(
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes,
                index=index,
                channel_period=20,
                adx_period=14,
                adx_threshold=20.0,
                volume_ratio_threshold=1.10,
            )
            if direction == "hold":
                continue
            atr_pct = (metrics.get("atr", 0.0) / closes[index]) if closes[index] > 0 else 0.0
            stop_loss_pct = max(atr_pct * 1.0, 0.0045)
            take_profit_pct = max(atr_pct * 1.8, stop_loss_pct * 1.6)
            returns.append(
                _simulate_intraday_trade(
                    closes,
                    entry_index=index,
                    direction=direction,
                    max_hold_bars=6,
                    take_profit_pct=take_profit_pct,
                    stop_loss_pct=stop_loss_pct,
                )
            )

        sample_count = max(len(closes) - start_index, 0)
        return build_backtest_snapshot(
            sample_count=sample_count,
            returns=returns,
            summary_prefix="donchian_adx_replay",
            empty_summary="donchian/adx replay found no valid setups in recent candles",
        )
