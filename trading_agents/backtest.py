from __future__ import annotations

from statistics import fmean

from trading_agents.models import BacktestSnapshot, MarketSnapshot, SentimentSnapshot


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
        # Replay a simplified version of the momentum strategy on recent candles.
        for index in range(20, len(closes) - 1):
            short_avg = fmean(closes[index - 4 : index + 1])
            long_avg = fmean(closes[index - 19 : index + 1])
            if not long_avg:
                continue
            momentum = (short_avg - long_avg) / long_avg
            next_return = (closes[index + 1] - closes[index]) / closes[index]

            if momentum > 0.002 and sentiment.sentiment_score >= -0.35:
                returns.append(next_return)
            elif momentum < -0.002 and sentiment.sentiment_score <= 0.45:
                returns.append(-next_return)

        sample_count = max(len(closes) - 21, 0)
        return build_backtest_snapshot(
            sample_count=sample_count,
            returns=returns,
            summary_prefix="replay",
            empty_summary="replay found no valid setups in recent candles",
        )
