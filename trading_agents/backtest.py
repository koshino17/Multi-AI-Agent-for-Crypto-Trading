from __future__ import annotations

from statistics import fmean

from trading_agents.models import BacktestSnapshot, MarketSnapshot, SentimentSnapshot


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
        if len(closes) < 26:
            return BacktestSnapshot(
                sample_count=0,
                trade_count=0,
                win_rate=0.0,
                avg_return_pct=0.0,
                cumulative_return_pct=0.0,
                summary="not enough candles for replay test",
            )

        returns: list[float] = []
        # Replay a compact intraday breakout rule with time-limited exits.
        for index in range(20, len(closes) - 4):
            short_avg = fmean(closes[index - 4:index + 1])
            long_avg = fmean(closes[index - 19:index + 1])
            if not long_avg:
                continue
            momentum = (short_avg - long_avg) / long_avg
            recent_volume = fmean(snapshot.volumes[max(0, index - 2): index + 1]) if snapshot.volumes else 0.0
            baseline_volume = fmean(snapshot.volumes[max(0, index - 19): index + 1]) if snapshot.volumes else 0.0
            volume_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
            rolling_high = max(closes[index - 9:index + 1])
            rolling_low = min(closes[index - 9:index + 1])

            if closes[index] >= rolling_high and volume_ratio >= 1.05 and sentiment.sentiment_score >= -0.55:
                returns.append(
                    _simulate_intraday_trade(
                        closes,
                        entry_index=index,
                        direction="long",
                        max_hold_bars=4,
                        take_profit_pct=0.009,
                        stop_loss_pct=0.0045,
                    )
                )
            elif closes[index] <= rolling_low and volume_ratio >= 1.05 and sentiment.sentiment_score <= 0.65:
                returns.append(
                    _simulate_intraday_trade(
                        closes,
                        entry_index=index,
                        direction="short",
                        max_hold_bars=4,
                        take_profit_pct=0.009,
                        stop_loss_pct=0.0045,
                    )
                )
            elif abs(momentum) >= 0.0028 and abs((closes[index] - short_avg) / short_avg) >= 0.0035:
                direction = "short" if closes[index] > short_avg else "long"
                returns.append(
                    _simulate_intraday_trade(
                        closes,
                        entry_index=index,
                        direction=direction,
                        max_hold_bars=3,
                        take_profit_pct=0.006,
                        stop_loss_pct=0.0035,
                    )
                )

        sample_count = max(len(closes) - 21, 0)
        return build_backtest_snapshot(
            sample_count=sample_count,
            returns=returns,
            summary_prefix="intraday_replay",
            empty_summary="intraday replay found no valid setups in recent candles",
        )
