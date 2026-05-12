from __future__ import annotations

import unittest
from types import SimpleNamespace

from trading_agents.agents import RiskSupervisorAgent
from trading_agents.models import BacktestSnapshot, SentimentSnapshot, StrategyCandidate, StrategyResearchSnapshot, TradeIdea
from trading_agents.runner import _monitor_snapshot
from trading_agents_web import _runtime_settings


class RuntimeRegressionTests(unittest.TestCase):
    def test_pilot_mode_review_uses_warnings_after_initialization(self) -> None:
        agent = RiskSupervisorAgent(llm_client=None)
        idea = TradeIdea("buy", 0.8, "test buy", "invalidate", "intraday")
        sentiment = SentimentSnapshot(2, 0.1, "ok", [])
        fallback_backtest = BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no replay")
        selected_backtest = BacktestSnapshot(10, 3, 0.66, 0.2, 0.6, "selected replay", 0.4, -0.2, 0.2, 2.0)
        strategy_research = StrategyResearchSnapshot(
            base_strategy_id="grid_range_reversion_maker_v1",
            selected_strategy_id="grid_range_reversion_maker_v1",
            selected_strategy_name="Grid",
            summary="selected grid maker",
            candidates=[
                StrategyCandidate(
                    strategy_id="grid_range_reversion_maker_v1",
                    name="Grid",
                    source="research",
                    credibility="experimental",
                    description="maker grid",
                    backtest=selected_backtest,
                )
            ],
            selected_execution_profile={"entry_order_type": "limit", "entry_liquidity": "maker"},
        )

        approval = agent.review(
            idea=idea,
            sentiment=sentiment,
            backtest=fallback_backtest,
            strategy_research=strategy_research,
            available_usdt=100.0,
            available_base_asset=0.0,
            position_side="flat",
            last_price=95.0,
            min_order_value_usdt=5.0,
            min_signal_score=0.55,
            max_position_pct=0.40,
            trading_mode="bybit-demo-perp",
            aggressive_mode=False,
            expectancy_floor_pct=-0.03,
            taker_fee_pct=0.001,
            buy_balance_buffer_pct=0.95,
            fee_hurdle_multiplier=1.15,
            cycle_mode="full",
            strategy_memory={
                "controls": {
                    "entry_mode": "capital_preservation_pilot",
                    "pilot_candidate_id": "grid_range_reversion_maker_v1",
                    "pilot_max_position_pct": 0.10,
                }
            },
            use_llm=False,
            total_equity_usdt=100.0,
            current_position_notional_usdt=0.0,
            current_leverage=0.0,
            liq_price=0.0,
            position_mm_usdt=0.0,
            perp_max_leverage=2.0,
            perp_min_available_balance_ratio_pct=10.0,
            perp_min_liquidation_buffer_pct=8.0,
        )

        self.assertTrue(any("capital-preservation pilot active" in item for item in approval.warnings))

    def test_runtime_settings_respect_web_form_overrides(self) -> None:
        effective = _runtime_settings("bybit-demo-perp", "SOL/USDT,BTC/USDT", "15")
        self.assertEqual(effective.trading_mode, "bybit-demo-perp")
        self.assertEqual(effective.symbol, "SOL/USDT")
        self.assertEqual(effective.observation_pool, ("SOL/USDT", "BTC/USDT"))
        self.assertEqual(effective.monitor_interval_seconds, 15.0)

    def test_monitor_snapshot_tracks_perp_net_position(self) -> None:
        class FakeExchange:
            def fetch_snapshot(self, symbol: str, timeframe: str, include_microstructure: bool = False):
                return SimpleNamespace(last_price=95.0)

            def fetch_account_state(self, symbol: str):
                return SimpleNamespace(free_usdt=12.5, base_asset=1.25, net_position=-2.5)

        snapshot = _monitor_snapshot(FakeExchange(), ["SOL/USDT"], "15m")
        self.assertEqual(snapshot["accounts"]["SOL/USDT"], (12.5, -2.5))


if __name__ == "__main__":
    unittest.main()
