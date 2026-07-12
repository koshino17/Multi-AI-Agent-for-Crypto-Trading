from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from trading_agents.agents import RiskSupervisorAgent, StrategistAgent, StrategyReflectionAgent
from trading_agents.exchange import _build_fvg_features, _build_microstructure_features, _infer_po3_phase_hint
from trading_agents.llm import _trace_date_label
from trading_agents.main import (
    _guard_market_structure_false_breakout,
    _prefilter_untradeable_candidate,
    _resolve_daily_review,
)
from trading_agents.models import BacktestSnapshot, SentimentSnapshot, StrategyCandidate, StrategyResearchSnapshot, TradeIdea
from trading_agents.reporting import (
    _build_financial_snapshot,
    _build_trade_review,
    _load_runner_event_counts,
    write_ground_truth_artifacts,
    write_oracle_postmortem_artifacts,
)
from trading_agents.research import StrategyResearchAgent
from trading_agents.runner import _acquire_runner_lock, _cycle_report_summary, _monitor_snapshot, _release_runner_lock
from trading_agents.service_manager import _runner_launch_agent_plist
from trading_agents.storage import build_storage_layout, mode_storage_root
from trading_agents_web import _runtime_settings


class RuntimeRegressionTests(unittest.TestCase):
    def test_mode_storage_root_keeps_bybit_live_at_canonical_root_and_scopes_mock(self) -> None:
        self.assertEqual(
            mode_storage_root("/tmp/tradepulse-state", "bybit-demo-perp"),
            Path("/tmp/tradepulse-state"),
        )
        self.assertEqual(
            mode_storage_root("/tmp/tradepulse-state", "mock"),
            Path("/tmp/tradepulse-state/modes/mock"),
        )
        self.assertEqual(
            mode_storage_root("/tmp/tradepulse-state/modes/mock", "mock"),
            Path("/tmp/tradepulse-state/modes/mock"),
        )

    def test_runner_launch_agent_uses_env_driven_launcher(self) -> None:
        plist = _runner_launch_agent_plist(
            Path("/tmp/tradepulse-runtime"),
            Path("/tmp/tradepulse-launchd.log"),
        )
        self.assertIn("/bin/zsh", plist)
        self.assertIn("/tmp/tradepulse-runtime/scripts/launch_trading_runner.sh", plist)
        self.assertNotIn("--mode", plist)
        self.assertNotIn("--symbol", plist)
        self.assertNotIn("bybit-demo-perp", plist)
        self.assertNotIn("mock", plist)

    def test_runner_launcher_uses_mode_scoped_storage(self) -> None:
        launcher = Path("scripts/launch_trading_runner.sh").read_text()
        self.assertIn("mode_storage_root", launcher)
        self.assertIn("settings.trading_mode", launcher)
        self.assertIn('"TRADING_MODE"', launcher)
        self.assertIn("refused to start", launcher)
        self.assertNotIn('exec >> "$RUNNER_LOG"', launcher)

    def test_runner_lock_blocks_duplicate_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = build_storage_layout(tmpdir)
            first_fd = _acquire_runner_lock(storage.runner_lock)
            self.assertIsNotNone(first_fd)
            try:
                self.assertIsNone(_acquire_runner_lock(storage.runner_lock))
            finally:
                _release_runner_lock(first_fd, storage.runner_lock)
            second_fd = _acquire_runner_lock(storage.runner_lock)
            try:
                self.assertIsNotNone(second_fd)
            finally:
                _release_runner_lock(second_fd, storage.runner_lock)

    def test_runner_event_counts_skip_large_non_event_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner_log = Path(tmpdir) / "runner.log"
            huge_report_line = json.dumps({"mode": "bybit-demo-perp", "blob": "x" * 1_000_000})
            rows = [
                huge_report_line,
                json.dumps({"event": "monitor", "timestamp": "2026-05-28T15:00:00+00:00"}),
                json.dumps({"event": "cycle", "status": "started", "timestamp": "2026-05-28T15:01:00+00:00"}),
                json.dumps({"event": "cycle", "status": "finished", "timestamp": "2026-05-28T15:01:30+00:00"}),
            ]
            runner_log.write_text("\n".join(rows) + "\n")
            counts = _load_runner_event_counts(runner_log, "2026-05-29")
        self.assertEqual(counts["monitor_heartbeats"], 1)
        self.assertEqual(counts["avg_decision_latency_seconds"], 30.0)

    def test_cycle_report_summary_omits_large_payloads(self) -> None:
        summary = _cycle_report_summary(
            {
                "mode": "bybit-demo-perp",
                "selected_symbol": "SOL/USDT",
                "cycle_mode": "full",
                "cycle_reason": "test",
                "idea": {"action": "hold", "score": 0.4},
                "approval": {"approved": False},
                "decision_source": "base_strategy",
                "trade_log": "/tmp/decision.json",
                "external_benchmarks": {"large": "x" * 1_000_000},
                "candidates": [{"strategy_research": {"selected_execution_profile": {"entry_ttl_seconds": 90}}}],
            }
        )
        self.assertEqual(summary["event"], "cycle_report")
        self.assertEqual(summary["entry_ttl_seconds"], 90)
        self.assertNotIn("external_benchmarks", summary)

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

    def test_cash_heavy_starter_long_requires_live_long_signal_or_countertrend_confirmation(self) -> None:
        agent = StrategistAgent(llm_client=None)
        fallback = agent._fallback_idea(
            momentum=0.0,
            sentiment=SentimentSnapshot(2, -0.76, "fear", []),
            backtest=BacktestSnapshot(20, 5, 1.0, 0.57, 2.83, "baseline replay", 0.57, 0.0, 0.57, 999.0),
            strategy_research=StrategyResearchSnapshot(
                base_strategy_id="grid_range_reversion_maker_v1",
                selected_strategy_id="donchian_adx_perp_v1",
                selected_strategy_name="Donchian ADX Perp",
                summary="selected strategy hold",
                candidates=[
                    StrategyCandidate(
                        strategy_id="donchian_adx_perp_v1",
                        name="Donchian ADX Perp",
                        source="public_classic",
                        credibility="external_public",
                        description="trend-following strategy",
                        backtest=BacktestSnapshot(
                            7,
                            7,
                            0.71,
                            0.21,
                            1.46,
                            "selected replay",
                            0.40,
                            -0.27,
                            0.21,
                            3.72,
                        ),
                    )
                ],
                current_signal="hold",
                current_signal_type="hold",
                current_volume_ratio=0.26,
            ),
            order_flow_bias=-0.32,
            order_flow_summary="spread=1.44bps; depth fairly balanced; trade_delta=-0.92; aggressive sellers in control",
            available_usdt=444.60,
            available_base_asset=0.0,
            position_side="flat",
            last_price=69.54,
            min_order_value_usdt=5.0,
            aggressive_mode=True,
            trading_mode="bybit-demo-perp",
        )
        self.assertEqual(fallback.action, "hold")

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

    def test_low_sample_guard_does_not_stack_base_only_on_top_of_pilot(self) -> None:
        agent = StrategyReflectionAgent(llm_client=None)
        guarded = agent._apply_low_sample_guard(  # type: ignore[attr-defined]
            {
                "entry_mode": "capital_preservation_pilot",
                "pilot_candidate_id": "grid_range_reversion_maker_v1",
                "pilot_max_position_pct": 0.10,
                "fallback_entry_mode": "base_only",
            },
            {
                "entry_mode": "capital_preservation_pilot",
                "pilot_candidate_id": "grid_range_reversion_maker_v1",
                "pilot_max_position_pct": 0.10,
                "fallback_entry_mode": "base_only",
            },
            accepted_orders=0,
            closed_episode_count=0,
        )
        normalized = agent._normalize_controls(  # type: ignore[attr-defined]
            guarded,
            {},
            reflection_context={},
        )
        self.assertEqual(str(normalized.get("entry_mode", "")), "capital_preservation_pilot")
        self.assertEqual(str(normalized.get("fallback_entry_mode", "")), "normal")

    def test_normalize_controls_recovers_missing_pilot_candidate_from_previous_experiment(self) -> None:
        agent = StrategyReflectionAgent(llm_client=None)
        normalized = agent._normalize_controls(  # type: ignore[attr-defined]
            {
                "entry_mode": "capital_preservation_pilot",
                "pilot_max_position_pct": 0.10,
                "benchmark_watch_candidate": "grid_range_reversion_maker_v1",
            },
            {},
            reflection_context={
                "previous_controls": {
                    "entry_mode": "capital_preservation_pilot",
                },
                "previous_experiment": {
                    "control_deltas": {
                        "pilot_candidate_id": {
                            "previous": None,
                            "current": "grid_range_reversion_maker_v1",
                        }
                    }
                },
            },
        )
        self.assertEqual(str(normalized.get("entry_mode", "")), "capital_preservation_pilot")
        self.assertEqual(str(normalized.get("pilot_candidate_id", "")), "grid_range_reversion_maker_v1")

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

    def test_fee_hurdle_multiplier_applies_in_bybit_demo_perp(self) -> None:
        agent = RiskSupervisorAgent(llm_client=None)
        idea = TradeIdea("buy", 0.8, "test buy", "invalidate", "intraday")
        sentiment = SentimentSnapshot(2, 0.1, "ok", [])
        fallback_backtest = BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no replay")
        selected_backtest = BacktestSnapshot(10, 3, 0.66, 0.2, 0.6, "selected replay", 0.4, -0.2, 0.12, 2.0)
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
            strategy_memory={"controls": {}},
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
        self.assertFalse(approval.approved)
        self.assertIn("expected edge below fee hurdle", approval.reason)

    def test_fast_short_flip_can_override_hold_bias(self) -> None:
        agent = StrategistAgent(llm_client=None)
        sentiment = SentimentSnapshot(2, 0.0, "balanced", [])
        baseline_backtest = BacktestSnapshot(6, 2, 0.5, 0.0, 0.0, "baseline replay", 0.3, -0.2, 0.01, 1.05)
        selected_backtest = BacktestSnapshot(8, 3, 0.55, 0.1, 0.8, "selected replay", 0.4, -0.2, 0.05, 1.25)
        strategy_research = StrategyResearchSnapshot(
            base_strategy_id="donchian_adx_keltner_v1",
            selected_strategy_id="donchian_adx_keltner_v1",
            selected_strategy_name="Donchian Keltner",
            summary="selected bearish continuation",
            candidates=[
                StrategyCandidate(
                    strategy_id="donchian_adx_keltner_v1",
                    name="Donchian Keltner",
                    source="research",
                    credibility="experimental",
                    description="trend strategy",
                    backtest=selected_backtest,
                )
            ],
            current_signal="short",
            current_volume_ratio=1.2,
        )
        idea = agent._fallback_idea(  # type: ignore[attr-defined]
            momentum=-0.0018,
            sentiment=sentiment,
            backtest=baseline_backtest,
            strategy_research=strategy_research,
            order_flow_bias=-0.12,
            order_flow_summary="ask-side depth dominates",
            available_usdt=100.0,
            available_base_asset=8.0,
            position_side="long",
            last_price=95.0,
            min_order_value_usdt=5.0,
            aggressive_mode=False,
            trading_mode="bybit-demo-perp",
        )
        self.assertEqual(idea.action, "sell")
        self.assertIn("fast short flip", idea.rationale)

    def test_aligned_add_on_relaxes_available_balance_guard(self) -> None:
        agent = RiskSupervisorAgent(llm_client=None)
        idea = TradeIdea("sell", 0.82, "trend add-on", "invalidate", "intraday")
        sentiment = SentimentSnapshot(2, -0.1, "ok", [])
        fallback_backtest = BacktestSnapshot(0, 0, 0.0, 0.0, 0.0, "no replay")
        selected_backtest = BacktestSnapshot(12, 4, 0.58, 0.2, 1.1, "selected replay", 0.5, -0.2, 0.15, 1.35)
        strategy_research = StrategyResearchSnapshot(
            base_strategy_id="donchian_adx_keltner_v1",
            selected_strategy_id="donchian_adx_keltner_v1",
            selected_strategy_name="Donchian Keltner",
            summary="selected bearish continuation",
            candidates=[
                StrategyCandidate(
                    strategy_id="donchian_adx_keltner_v1",
                    name="Donchian Keltner",
                    source="research",
                    credibility="experimental",
                    description="trend strategy",
                    backtest=selected_backtest,
                )
            ],
            current_signal="short",
            current_volume_ratio=1.2,
            selected_execution_profile={"entry_order_type": "market", "entry_liquidity": "taker"},
        )

        approval = agent.review(
            idea=idea,
            sentiment=sentiment,
            backtest=fallback_backtest,
            strategy_research=strategy_research,
            available_usdt=14.0,
            available_base_asset=8.0,
            position_side="short",
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
            strategy_memory={"controls": {}},
            use_llm=False,
            total_equity_usdt=100.0,
            current_position_notional_usdt=70.0,
            current_leverage=0.7,
            liq_price=0.0,
            position_mm_usdt=0.0,
            perp_max_leverage=2.0,
            perp_min_available_balance_ratio_pct=10.0,
            perp_min_liquidation_buffer_pct=8.0,
        )
        self.assertTrue(approval.approved)
        self.assertTrue(any("trend add-on balance guard relaxed" in item for item in approval.warnings))

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

    def test_daily_review_fallback_error_is_persisted_without_repeat_for_same_fingerprint(self) -> None:
        class StubReviewer:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate_with_metadata(self, date_label: str, daily_summary: dict):
                from trading_agents.models import DailyReviewSnapshot

                self.calls += 1
                return (
                    DailyReviewSnapshot(
                        title=f"Trading Agents Daily Review - {date_label}",
                        operations_summary="ops",
                        decision_summary="decision",
                        improvement_directions=["one"],
                        action_items=["two"],
                    ),
                    {"review_status": "fallback_error", "review_error": "timeout", "used_fallback": True},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            storage = SimpleNamespace(service=root)
            summary = {
                "financial_snapshot": {"daily_pnl_usdt": -1.0, "realized_pnl_usdt": 0.0, "unrealized_pnl_usdt": 0.0},
                "loss_attribution": {},
                "symbol_postmortem": {"symbol": "SOL/USDT"},
                "external_benchmarks": {},
                "strategy_memory_current": {},
            }
            reviewer = StubReviewer()
            first = _resolve_daily_review(
                storage=storage,
                date_label="2026-05-19",
                daily_summary=summary,
                daily_reviewer=reviewer,
            )
            second = _resolve_daily_review(
                storage=storage,
                date_label="2026-05-19",
                daily_summary=summary,
                daily_reviewer=reviewer,
            )
            self.assertEqual(reviewer.calls, 1)
            self.assertEqual(first.get("review_status"), "fallback_error")
            self.assertEqual(second.get("review_status"), "fallback_error")

    def test_prefilter_blocks_negative_edge_candidate_before_risk(self) -> None:
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
                    backtest=BacktestSnapshot(
                        sample_count=100,
                        trade_count=6,
                        win_rate=0.33,
                        avg_return_pct=-0.08,
                        cumulative_return_pct=-0.12,
                        summary="weak candidate",
                        avg_win_pct=0.10,
                        avg_loss_pct=-0.14,
                        expectancy_pct=-0.05,
                        profit_factor=0.70,
                    ),
                )
            ],
        )
        idea, reason = _prefilter_untradeable_candidate(
            idea=TradeIdea("buy", 0.71, "try long", "invalidate", "intraday"),
            strategy_research=strategy_research,
            position_side="flat",
            mode="bybit-demo-perp",
            aggressive_mode=False,
            policy_exit=False,
        )
        self.assertEqual(idea.action, "hold")
        self.assertIn("candidate prefiltered before risk", reason)

    def test_prefilter_blocks_low_sample_candidate_without_positive_edge(self) -> None:
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
                    backtest=BacktestSnapshot(
                        sample_count=50,
                        trade_count=4,
                        win_rate=0.50,
                        avg_return_pct=0.01,
                        cumulative_return_pct=0.04,
                        summary="tiny sample",
                        avg_win_pct=0.10,
                        avg_loss_pct=-0.08,
                        expectancy_pct=0.00,
                        profit_factor=1.05,
                    ),
                )
            ],
        )
        idea, reason = _prefilter_untradeable_candidate(
            idea=TradeIdea("buy", 0.68, "test long", "invalidate", "intraday"),
            strategy_research=strategy_research,
            position_side="flat",
            mode="bybit-demo-perp",
            aggressive_mode=False,
            policy_exit=False,
        )
        self.assertEqual(idea.action, "hold")
        self.assertIn("low-sample replay weak", reason)

    def test_strategy_reflection_raises_cooldown_when_recent_realized_after_fees_stays_negative(self) -> None:
        agent = StrategyReflectionAgent(llm_client=None)
        daily_summary = {
            "blocked_reason_counts": {},
            "rejection_reason_counts": {},
            "selected_symbol_counts": {"SOL/USDT": 20},
            "financial_snapshot": {
                "daily_pnl_usdt": -0.6,
                "realized_pnl_usdt": 0.0,
                "unrealized_pnl_usdt": 0.0,
                "daily_fees_usdt": 0.3,
            },
            "accepted_source_counts": {"fallback": 0, "base_strategy": 2},
            "accepted_orders": 2,
            "blocked": 0,
            "loss_attribution": {
                "closed_episode_count": 2,
                "realized_after_fees_usdt": -0.32,
            },
            "external_benchmarks": {"top_candidates": [{}]},
        }
        reflection_context = {
            "lookback_days": 5,
            "negative_day_count": 4,
            "negative_streak": 1,
            "positive_streak": 0,
            "low_participation_window_count": 0,
            "low_participation_streak": 0,
            "carry_in_loss_window_count": 0,
            "carry_in_loss_streak": 0,
            "stagnation_exit_window_count": 0,
            "stagnation_exit_streak": 0,
            "multi_day_pnl_usdt": -44.0,
            "current_equity_usdt": 455.0,
            "configured_initial_usdt": 500.0,
            "live_trade_expectancy_pct": -0.01,
            "live_profit_factor": 0.40,
            "restore_positive_days": 2,
            "restore_equity_floor_usdt": 475.0,
            "force_fallback_base_only": True,
            "capital_preservation_mode": False,
            "live_symbols": ["SOL/USDT"],
            "current_live_symbol": "SOL/USDT",
            "live_symbol_benchmark": {},
            "strategy_research_recommendation": {},
            "previous_controls": {"cooldown_scale": 0.5, "fallback_entry_mode": "base_only"},
            "previous_experiment": {},
            "current_window_accepted_orders": 2,
            "current_window_closed_episodes": 2,
            "recent_windows": [
                {"realized_after_fees_usdt": -0.19},
                {"realized_after_fees_usdt": -0.32},
            ],
        }
        reflection = agent.evaluate("2026-05-20-day", daily_summary, reflection_context=reflection_context)
        self.assertGreaterEqual(float(reflection.controls.get("cooldown_scale", 0.0) or 0.0), 0.75)

    def test_microstructure_features_include_value_profile_fvg_and_po3_hints(self) -> None:
        features = _build_microstructure_features(
            bids=[[99.9, 10], [99.8, 8]],
            asks=[[100.1, 9], [100.2, 7]],
            trades=[
                {"price": 100.0, "size": 2.0, "side": "Buy"},
                {"price": 99.95, "size": 1.5, "side": "Sell"},
            ],
            last_price=100.0,
            highs=[100.0, 100.1, 100.2, 100.3, 100.4, 100.45, 100.5, 100.55, 100.6, 100.65, 100.7, 100.75],
            lows=[99.8, 99.85, 99.9, 99.95, 100.0, 100.05, 100.1, 100.15, 100.2, 100.25, 100.5, 100.55],
            closes=[99.9, 99.95, 100.0, 100.05, 100.1, 100.15, 100.2, 100.25, 100.3, 100.35, 100.6, 100.62],
            volumes=[10, 12, 11, 14, 13, 15, 16, 12, 13, 14, 20, 18],
        )
        self.assertIn("poc_price", features)
        self.assertIn("value_area_high_price", features)
        self.assertIn("value_area_low_price", features)
        self.assertIn("nearest_bullish_fvg_distance_bps", features)
        self.assertIn("nearest_bearish_fvg_distance_bps", features)
        self.assertIn("fvg_fill_ratio", features)
        self.assertIn("po3_phase_hint", features)
        self.assertTrue(str(features["po3_phase_hint"]))

    def test_market_structure_guard_blocks_weak_buy_above_value_area(self) -> None:
        idea, reason = _guard_market_structure_false_breakout(
            idea=TradeIdea("buy", 0.72, "test breakout long", "invalidate", "intraday"),
            snapshot=SimpleNamespace(
                po3_phase_hint="manipulation_up",
                value_area_high_distance_bps=-12.0,
                value_area_low_distance_bps=-35.0,
                nearest_bearish_fvg_distance_bps=18.0,
                nearest_bullish_fvg_distance_bps=-120.0,
                fvg_fill_ratio=0.10,
                trade_delta_ratio=0.05,
            ),
            strategy_research=SimpleNamespace(current_signal="hold", current_volume_ratio=0.95),
            llm_wake={"metrics": {"trade_delta_ratio": 0.05, "volume_ratio": 0.95}},
            position_side="flat",
            mode="bybit-demo-perp",
            settings=SimpleNamespace(
                market_structure_guard_enabled=True,
                market_structure_guard_value_area_breach_bps=8.0,
                market_structure_guard_fvg_near_bps=30.0,
                market_structure_guard_trade_delta_ratio=0.20,
                market_structure_guard_volume_ratio=1.10,
                market_structure_guard_fill_ratio=0.25,
            ),
        )
        self.assertEqual(idea.action, "hold")
        self.assertIn("market-structure guard", reason)

    def test_infer_po3_phase_hint_accumulation_for_compressed_recent_range(self) -> None:
        highs = [100.0, 110.0, 120.0, 118.0, 104.8, 104.6, 104.4, 104.2, 104.1, 104.0, 103.95, 103.9]
        lows = [80.0, 82.0, 84.0, 86.0, 103.5, 103.6, 103.7, 103.8, 103.85, 103.9, 103.88, 103.87]
        closes = [90.0, 95.0, 100.0, 105.0, 104.0, 104.05, 104.1, 104.0, 104.02, 104.01, 104.0, 103.99]
        phase = _infer_po3_phase_hint(last_price=103.99, highs=highs, lows=lows, closes=closes)
        self.assertEqual(phase, "accumulation")

    def test_infer_po3_phase_hint_expansion_down_for_selloff_near_recent_low(self) -> None:
        highs = [104.0, 103.8, 103.5, 103.2, 103.0, 102.8, 102.5, 102.0, 101.8, 101.3, 100.9, 100.6]
        lows = [103.4, 103.1, 102.8, 102.4, 102.0, 101.8, 101.3, 100.9, 100.5, 100.1, 99.8, 99.4]
        closes = [103.6, 103.2, 102.9, 102.5, 102.2, 101.9, 101.5, 101.1, 100.8, 100.3, 99.9, 99.45]
        phase = _infer_po3_phase_hint(last_price=99.45, highs=highs, lows=lows, closes=closes)
        self.assertEqual(phase, "expansion_down")

    def test_build_fvg_features_detects_bullish_gap_and_fill_ratio(self) -> None:
        features = _build_fvg_features(
            last_price=104.5,
            highs=[100.0, 101.0, 102.0, 103.0, 104.0],
            lows=[99.0, 99.5, 102.8, 103.4, 104.2],
        )
        self.assertNotEqual(features["nearest_bullish_fvg_distance_bps"], 0.0)
        self.assertGreaterEqual(features["fvg_fill_ratio"], 0.0)
        self.assertLessEqual(features["fvg_fill_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
