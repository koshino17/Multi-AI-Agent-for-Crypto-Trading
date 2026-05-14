from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from trading_agents.agents import RiskSupervisorAgent, StrategyReflectionAgent
from trading_agents.llm import _trace_date_label
from trading_agents.models import BacktestSnapshot, SentimentSnapshot, StrategyCandidate, StrategyResearchSnapshot, TradeIdea
from trading_agents.reporting import (
    _build_financial_snapshot,
    _build_trade_review,
    write_ground_truth_artifacts,
    write_oracle_postmortem_artifacts,
)
from trading_agents.research import StrategyResearchAgent
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

    def test_strategy_reflection_low_sample_guard_limits_tunable_control_churn(self) -> None:
        agent = StrategyReflectionAgent(llm_client=None)
        daily_summary = {
            "blocked_reason_counts": {"symbol cooldown active": 20},
            "rejection_reason_counts": {},
            "selected_symbol_counts": {"SOL/USDT": 10},
            "financial_snapshot": {
                "daily_pnl_usdt": -1.5,
                "realized_pnl_usdt": 0.0,
                "unrealized_pnl_usdt": 0.0,
                "daily_fees_usdt": 0.1,
            },
            "accepted_source_counts": {"fallback": 0, "base_strategy": 0},
            "accepted_orders": 0,
            "blocked": 20,
            "loss_attribution": {"closed_episode_count": 0},
            "external_benchmarks": {"top_candidates": [{}]},
        }
        reflection_context = {
            "live_symbols": ["SOL/USDT"],
            "current_live_symbol": "SOL/USDT",
            "lookback_days": 5,
            "negative_day_count": 3,
            "negative_streak": 2,
            "positive_streak": 0,
            "carry_in_loss_window_count": 2,
            "carry_in_loss_streak": 2,
            "stagnation_exit_window_count": 2,
            "stagnation_exit_streak": 2,
            "previous_controls": {
                "cooldown_scale": 1.0,
                "hold_bars_scale": 1.0,
                "stagnation_bars_scale": 1.0,
                "stagnation_pnl_scale": 1.0,
            },
            "current_window_accepted_orders": 0,
            "current_window_closed_episodes": 0,
            "strategy_research_recommendation": {},
            "live_symbol_benchmark": {},
        }
        reflection = agent.evaluate("2026-05-12-day", daily_summary, reflection_context=reflection_context)
        self.assertAlmostEqual(float(reflection.controls.get("cooldown_scale", 1.0)), 0.85, places=2)
        self.assertAlmostEqual(float(reflection.controls.get("hold_bars_scale", 1.0)), 1.0, places=2)
        self.assertTrue(bool(reflection.experiment.get("sample_guard_active")))

    def test_strategy_reflection_clears_stale_pilot_controls_when_not_reaffirmed(self) -> None:
        agent = StrategyReflectionAgent(llm_client=None)
        daily_summary = {
            "blocked_reason_counts": {},
            "rejection_reason_counts": {},
            "selected_symbol_counts": {"SOL/USDT": 10},
            "financial_snapshot": {
                "daily_pnl_usdt": 0.0,
                "realized_pnl_usdt": 0.0,
                "unrealized_pnl_usdt": 0.0,
                "daily_fees_usdt": 0.0,
            },
            "accepted_source_counts": {"fallback": 0, "base_strategy": 0},
            "accepted_orders": 0,
            "blocked": 0,
            "loss_attribution": {"closed_episode_count": 0},
            "external_benchmarks": {"top_candidates": [{}]},
        }
        reflection_context = {
            "live_symbols": ["SOL/USDT"],
            "current_live_symbol": "SOL/USDT",
            "lookback_days": 5,
            "negative_day_count": 0,
            "negative_streak": 0,
            "positive_streak": 2,
            "low_participation_window_count": 3,
            "low_participation_streak": 3,
            "carry_in_loss_window_count": 0,
            "carry_in_loss_streak": 0,
            "stagnation_exit_window_count": 0,
            "stagnation_exit_streak": 0,
            "previous_controls": {
                "entry_mode": "capital_preservation_pilot",
                "pilot_candidate_id": "grid_range_reversion_maker_v1",
                "pilot_max_position_pct": 0.10,
            },
            "current_window_accepted_orders": 0,
            "current_window_closed_episodes": 0,
            "strategy_research_recommendation": {
                "candidate_id": "grid_range_reversion_maker_v1",
                "verdict": "research_only",
            },
            "live_symbol_benchmark": {
                "candidate_id": "bollinger_keltner_extreme_reversion_v1",
                "expectancy_pct": -0.01,
                "profit_factor": 0.95,
                "uses_custom_cost_model": False,
            },
        }
        reflection = agent.evaluate("2026-05-13-day", daily_summary, reflection_context=reflection_context)
        self.assertEqual(str(reflection.controls.get("entry_mode", "")), "capital_preservation_pilot")
        self.assertEqual(str(reflection.controls.get("pilot_candidate_id", "")), "grid_range_reversion_maker_v1")

    def test_strategy_research_uses_memory_to_bias_candidate_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            library_path = Path(tmpdir) / "strategy_library.json"
            library_path.write_text(
                json.dumps(
                    {
                        "base_strategy": "donchian_adx_perp_v1",
                        "strategies": [
                            {
                                "id": "donchian_adx_perp_v1",
                                "name": "Donchian",
                                "generator": "donchian_adx",
                                "execution": {"entry_order_type": "market", "entry_liquidity": "taker"},
                            },
                            {
                                "id": "grid_range_reversion_maker_v1",
                                "name": "Grid Maker",
                                "generator": "grid_range_reversion",
                                "execution": {"entry_order_type": "limit", "entry_liquidity": "maker"},
                            },
                        ],
                    }
                )
            )
            agent = StrategyResearchAgent(str(library_path))

            def fake_run_strategy(item, snapshot, sentiment):
                if item.get("id") == "donchian_adx_perp_v1":
                    return BacktestSnapshot(100, 10, 0.55, 0.10, 1.0, "donchian", 0.30, -0.15, 0.06, 1.30)
                return BacktestSnapshot(100, 10, 0.52, 0.08, 0.8, "grid", 0.25, -0.12, 0.05, 1.20)

            agent._run_strategy = fake_run_strategy  # type: ignore[method-assign]
            snapshot = SimpleNamespace(
                symbol="SOL/USDT",
                timeframe="15m",
                opens=[1.0] * 40,
                highs=[1.0] * 40,
                lows=[1.0] * 40,
                closes=[1.0] * 40,
                volumes=[1.0] * 40,
                last_price=1.0,
            )
            sentiment = SentimentSnapshot(0, 0.0, "", [])
            result = agent.evaluate_with_memory(
                snapshot,
                sentiment,
                strategy_memory={
                    "controls": {
                        "benchmark_watch_candidate": "grid_range_reversion_maker_v1",
                        "pilot_candidate_id": "grid_range_reversion_maker_v1",
                        "entry_mode": "capital_preservation_pilot",
                    }
                },
            )
            self.assertEqual(result.selected_strategy_id, "grid_range_reversion_maker_v1")

    def test_trace_date_label_uses_noon_anchor(self) -> None:
        before_noon = datetime(2026, 5, 12, 1, 0, tzinfo=timezone.utc)
        after_noon = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(_trace_date_label(before_noon), "2026-05-11")
        self.assertEqual(_trace_date_label(after_noon), "2026-05-12")

    def test_reporting_infers_unlogged_perp_close_from_account_transition(self) -> None:
        records = [
            {
                "mode": "bybit-demo-perp",
                "selected_symbol": "SOL/USDT",
                "last_price": 95.52,
                "__record_timestamp_local": "2026-05-13T12:01:06.261803+08:00",
                "account": {
                    "market_type": "perp",
                    "position_side": "flat",
                    "net_position": 0.0,
                    "entry_price": 0.0,
                    "mark_price": 0.0,
                    "total_equity_usdt": 452.0029,
                    "available_balance_usdt": 452.0029,
                    "cum_realized_pnl_usdt": 0.0,
                },
            },
            {
                "mode": "bybit-demo-perp",
                "selected_symbol": "SOL/USDT",
                "last_price": 91.47,
                "__record_timestamp_local": "2026-05-13T23:08:14.862564+08:00",
                "account": {
                    "market_type": "perp",
                    "position_side": "short",
                    "net_position": -8.6,
                    "entry_price": 92.759186,
                    "mark_price": 91.47,
                    "position_notional_usdt": 797.729,
                    "unrealized_pnl_usdt": 11.087,
                    "total_equity_usdt": 462.5378,
                    "available_balance_usdt": 68.7093,
                    "hold_minutes": 148.79,
                    "opened_at_local": "2026-05-13T20:38:36.862173+08:00",
                    "entry_count": 1,
                    "cum_realized_pnl_usdt": -52.0006,
                },
            },
            {
                "mode": "bybit-demo-perp",
                "selected_symbol": "SOL/USDT",
                "last_price": 91.30,
                "__record_timestamp_local": "2026-05-13T23:10:27.616561+08:00",
                "account": {
                    "market_type": "perp",
                    "position_side": "flat",
                    "net_position": 0.0,
                    "entry_price": 0.0,
                    "mark_price": 0.0,
                    "total_equity_usdt": 465.2268,
                    "available_balance_usdt": 465.2136,
                    "cum_realized_pnl_usdt": 0.0,
                },
            },
        ]
        trade_review = _build_trade_review(records, financial_snapshot={})
        closed = [item for item in trade_review["episodes"] if item.get("status") in {"win", "loss", "flat"}]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["entry_source"], "unlogged_in_window")
        self.assertEqual(closed[0]["close_source"], "account_state_inferred")
        financial = _build_financial_snapshot(
            records,
            records,
            initial_balance_usdt=500.0,
            taker_fee_pct=0.001,
            position_policy_metadata={},
        )
        self.assertGreater(float(financial["realized_pnl_usdt"]), 12.0)
        self.assertLess(abs(float(financial["pnl_bridge_residual_usdt"])), 1.2)

    def test_ground_truth_and_oracle_artifacts_write_expected_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            summary = {
                "date_label": "2026-05-12",
                "window_start": "2026-05-11T12:00:00+08:00",
                "window_end": "2026-05-12T12:00:00+08:00",
                "total": 100,
                "accepted_orders": 0,
                "holds": 90,
                "action_counts": {"hold": 90, "buy": 10},
                "executed_trade_timeline": [],
                "financial_snapshot": {
                    "daily_pnl_usdt": -1.2,
                    "daily_pnl_pct": -0.25,
                    "realized_pnl_usdt": -0.8,
                    "unrealized_pnl_usdt": 0.0,
                    "daily_fees_usdt": 0.2,
                },
                "market_path_review": {
                    "symbol": "SOL/USDT",
                    "summary": "rebound dominated the window",
                    "max_drawdown_pct": -1.5,
                    "max_rebound_pct": 2.2,
                    "max_drawdown_action_counts": {"hold": 8, "sell": 0},
                    "max_rebound_action_counts": {"hold": 20, "buy": 1},
                },
                "loss_attribution": {
                    "carry_in_closed_count": 1,
                    "focus_symbol_benchmark": {
                        "candidate_id": "grid_range_reversion_maker_v1",
                        "expectancy_pct": 0.12,
                        "profit_factor": 2.0,
                    },
                },
                "strategy_research_latest": {
                    "recommendation": {
                        "candidate_id": "grid_range_reversion_maker_v1",
                        "verdict": "shadow_candidate",
                    }
                },
                "shadow_benchmark_watch": {"watch_candidate_id": "grid_range_reversion_maker_v1"},
                "trade_review": {"episodes": []},
                "benchmark_watch_candidate_current": {},
            }
            gt_paths = write_ground_truth_artifacts(base, "2026-05-12", summary)
            oracle_paths = write_oracle_postmortem_artifacts(base, "2026-05-12", summary)
            self.assertTrue(Path(gt_paths["json_path"]).exists())
            self.assertTrue(Path(oracle_paths["json_path"]).exists())
            oracle_payload = json.loads(Path(oracle_paths["json_path"]).read_text())
            self.assertIn("carry_in_drag", oracle_payload["root_cause_tags"])
            self.assertIn("missed_rebound_participation", oracle_payload["root_cause_tags"])


if __name__ == "__main__":
    unittest.main()
