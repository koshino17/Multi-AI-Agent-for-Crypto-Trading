from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from trading_agents.mentor_review import (
    apply_autopromotion,
    evaluate_shadow_gate,
    extract_worst_trade_evidence,
    merge_provider_consensus,
    mentor_promotion_history_path,
    mentor_review_path,
    run_mentor_cycle,
    shadow_gate_path,
    shadow_prompt_path,
)
from trading_agents.reporting import build_daily_summary
from trading_agents.storage import build_storage_layout


class MentorReviewTests(unittest.TestCase):
    def test_worst_episodes_and_orders_top3_sort_and_fallback(self) -> None:
        trade_review = {
            "episodes": [
                {
                    "id": "open",
                    "status": "open",
                    "estimated_edge_pct": -9.0,
                    "opening_order": {"id": "ignored"},
                },
                self._episode("e1", -0.1, closing_order={"id": "close-1"}),
                self._episode("e2", -2.5, opening_order={"id": "open-2"}),
                self._episode("e3", 0.8, closing_order={"id": "close-3"}),
                self._episode("e4", -1.2, closing_order={"id": "close-4"}),
            ]
        }
        worst, orders = extract_worst_trade_evidence(trade_review)

        self.assertEqual([item["episode"]["id"] for item in worst], ["e2", "e4", "e1"])
        self.assertEqual([item["order"]["id"] for item in orders], ["open-2", "close-4", "close-1"])
        self.assertEqual(orders[0]["source"], "opening_order")
        self.assertIn("llm_wake", worst[0])
        self.assertIn("market_structure", worst[0])
        self.assertIn("execution_constraints", worst[0])
        self.assertIn("approval", worst[0])
        self.assertIn("debate", worst[0])
        self.assertIn("account", worst[0])
        self.assertIn("result", worst[0])
        self.assertEqual(worst[0]["idea"]["rationale"], "rationale e2")

    def test_provider_consensus_safe_conflict_and_missing_provider_health(self) -> None:
        payload_a = self._provider_payload({"entry_mode": "capital_preservation_pilot", "pilot_max_position_pct": 0.10})
        payload_b = self._provider_payload({"entry_mode": "capital_preservation_pilot", "pilot_max_position_pct": 0.20})
        consensus = merge_provider_consensus(
            {
                "strategist": [
                    {"provider": "openai", "model": "o", "status": "ok", "payload": payload_a},
                    {"provider": "gemini", "model": "g", "status": "ok", "payload": payload_b},
                ]
            },
            expected_providers=("openai", "gemini"),
        )

        self.assertEqual(consensus["safe_patch"]["controls_patch"]["strategist"]["entry_mode"], "capital_preservation_pilot")
        self.assertIn("pilot_max_position_pct", consensus["conflict_patch"]["controls_patch"]["strategist"])
        self.assertTrue(consensus["provider_health"]["all_required_ok"])

        missing = merge_provider_consensus(
            {"strategist": [{"provider": "openai", "model": "o", "status": "ok", "payload": payload_a}]},
            expected_providers=("openai", "gemini"),
        )
        self.assertFalse(missing["provider_health"]["all_required_ok"])

    def test_shadow_gate_pass_and_trade_count_fail(self) -> None:
        settings = self._settings()
        runs = self._gate_runs(candidate_trade_count=8)
        gate = evaluate_shadow_gate(
            settings=settings,
            storage=SimpleNamespace(),
            candidate_id="pilot_v1",
            focus_symbol="SOL/USDT",
            validation_symbols=("BTC/USDT", "ETH/USDT"),
            benchmark_runs=runs,
        )
        self.assertTrue(gate["pass"], gate["reasons"])

        failed = evaluate_shadow_gate(
            settings=settings,
            storage=SimpleNamespace(),
            candidate_id="pilot_v1",
            focus_symbol="SOL/USDT",
            validation_symbols=("BTC/USDT", "ETH/USDT"),
            benchmark_runs=self._gate_runs(candidate_trade_count=7),
        )
        self.assertFalse(failed["pass"])
        self.assertTrue(any("trade_count" in reason for reason in failed["reasons"]))

    def test_validation_guard_fail(self) -> None:
        settings = self._settings()
        runs = self._gate_runs(candidate_trade_count=8, validation_expectancy=-0.11)
        gate = evaluate_shadow_gate(
            settings=settings,
            storage=SimpleNamespace(),
            candidate_id="pilot_v1",
            focus_symbol="SOL/USDT",
            validation_symbols=("BTC/USDT", "ETH/USDT"),
            benchmark_runs=runs,
        )
        self.assertFalse(gate["pass"])
        self.assertTrue(any("validation expectancy" in reason for reason in gate["reasons"]))

    def test_autopromote_whitelist_blocks_non_live_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = build_storage_layout(tmpdir)
            consensus = {
                "safe_patch": {
                    "controls_patch": {
                        "strategist": {
                            "entry_mode": "capital_preservation_pilot",
                            "pilot_candidate_id": "pilot_v1",
                            "pilot_max_position_pct": 0.10,
                            "max_position_pct": 0.99,
                        }
                    }
                }
            }
            gate = {"pass": True, "candidate_id": "pilot_v1", "comparisons": []}
            promotion = apply_autopromotion(
                date_label="2026-07-08",
                storage=storage,
                consensus=consensus,
                gate=gate,
                autopromote_allowed=True,
                promote=True,
            )
            memory = json.loads(storage.strategy_memory_state.read_text())
            self.assertEqual(promotion["status"], "promoted")
            self.assertEqual(memory["controls"]["pilot_candidate_id"], "pilot_v1")
            self.assertNotIn("max_position_pct", memory["controls"])

    def test_run_cycle_artifacts_report_and_promotion_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = build_storage_layout(tmpdir)
            settings = self._settings()
            result = run_mentor_cycle(
                date_label="2026-07-08",
                daily_summary=self._daily_summary(),
                daily_review={"consensus_summary": "review"},
                settings=settings,
                storage=storage,
                mode="bybit-demo-perp",
                promote=True,
                provider_runner=self._provider_runner,
                benchmark_runs=self._gate_runs(candidate_trade_count=8),
            )

            self.assertEqual(result["status"], "updated")
            self.assertTrue(mentor_review_path(storage, "2026-07-08").exists())
            self.assertTrue(shadow_prompt_path(storage, "2026-07-08").exists())
            self.assertTrue(shadow_gate_path(storage, "2026-07-08").exists())
            self.assertTrue(mentor_promotion_history_path(storage).exists())
            memory = json.loads(storage.strategy_memory_state.read_text())
            self.assertEqual(memory["mentor_last_promotion"]["candidate"], "pilot_v1")
            self.assertIn("mentor_shadow_reference", memory)

            report = build_daily_summary(
                storage.trade_logs,
                "2026-07-08",
                storage.runner_log,
                trading_mode="bybit-demo-perp",
                storage_root=storage.root,
            )
            self.assertIn("## Mentor Review", report)
            self.assertIn("## Shadow Gate", report)
            self.assertIn("## Promotion", report)

    def test_run_cycle_no_promote_keeps_shadow_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = build_storage_layout(tmpdir)
            result = run_mentor_cycle(
                date_label="2026-07-08",
                daily_summary=self._daily_summary(),
                daily_review={},
                settings=self._settings(),
                storage=storage,
                mode="bybit-demo-perp",
                promote=False,
                provider_runner=self._provider_runner,
                benchmark_runs=self._gate_runs(candidate_trade_count=8),
            )
            self.assertFalse(result["autopromote_allowed"])
            self.assertFalse(storage.strategy_memory_state.exists())
            self.assertTrue(mentor_promotion_history_path(storage).exists())

    def _episode(self, episode_id: str, edge: float, **orders: dict) -> dict:
        return {
            "id": episode_id,
            "status": "closed",
            "estimated_edge_pct": edge,
            "llm_wake": {"metrics": {"score": 1}},
            "market_structure": {"po3_phase": "manipulation"},
            "execution_constraints": {"maker": True},
            "approval": {"approved": True},
            "debate": {"consensus": "buy"},
            "account": {"equity": 100},
            "result": {"status": "filled"},
            "idea": {"rationale": f"rationale {episode_id}"},
            **orders,
        }

    def _provider_payload(self, controls: dict) -> dict:
        return {
            "summary": "tighten pilot",
            "findings": ["one", "two", "three", "four"],
            "controls_patch": controls,
            "prompt_patch_structured": [{"op": "add_rule", "target": "strategist", "value": "prefer PO3 evidence"}],
            "benchmark_hypothesis": {"candidate_id": "pilot_v1"},
            "confidence": 0.8,
        }

    def _provider_runner(self, **_: object) -> list[dict]:
        payload = self._provider_payload(
            {
                "entry_mode": "capital_preservation_pilot",
                "pilot_candidate_id": "pilot_v1",
                "pilot_max_position_pct": 0.10,
                "unsafe_live_key": True,
            }
        )
        return [
            {"provider": "openai", "model": "o", "status": "ok", "payload": payload},
            {"provider": "gemini", "model": "g", "status": "ok", "payload": payload},
        ]

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            external_mentor_providers=("openai", "gemini"),
            external_mentor_openai_model="o",
            external_mentor_gemini_model="g",
            mentor_timeout_seconds=1.0,
            mentor_autopromote_enabled=True,
            mentor_gate_windows=("96", "320", "1000"),
            mentor_gate_min_trades=8,
            mentor_gate_min_expectancy_delta_96=0.03,
            mentor_gate_min_expectancy_delta_long=0.00,
            mentor_gate_min_pf_delta_96=0.00,
            mentor_gate_min_pf_delta_long=-0.05,
            mentor_gate_max_cum_return_gap_pct=0.50,
            observation_pool=("SOL/USDT",),
            strategy_research_validation_symbols=("BTC/USDT", "ETH/USDT"),
            timeframe="15m",
        )

    def _daily_summary(self) -> dict:
        return {
            "date_label": "2026-07-08",
            "mode": "bybit-demo-perp",
            "symbol_postmortem": {"symbol": "SOL/USDT"},
            "trade_review": {
                "episodes": [
                    self._episode("e1", -1.0, closing_order={"id": "close"}),
                    self._episode("e2", 0.5, opening_order={"id": "open"}),
                ]
            },
            "financial_snapshot": {"daily_pnl_usdt": -1.0},
        }

    def _gate_runs(self, *, candidate_trade_count: int, validation_expectancy: float = 0.02) -> list[dict]:
        runs: list[dict] = []
        for symbol in ("SOL/USDT", "BTC/USDT", "ETH/USDT"):
            for window in (96, 320, 1000):
                candidate_expectancy = 0.04 if symbol == "SOL/USDT" else validation_expectancy
                runs.append(
                    {
                        "symbol": symbol,
                        "window": window,
                        "baseline_strategy_id": "baseline",
                        "ranked_results": [
                            {
                                "candidate_id": "baseline",
                                "baseline": True,
                                "expectancy_pct": 0.00,
                                "profit_factor": 1.00,
                                "cumulative_return_pct": 0.00,
                                "trade_count": 20,
                            },
                            {
                                "candidate_id": "pilot_v1",
                                "baseline": False,
                                "expectancy_pct": candidate_expectancy,
                                "profit_factor": 1.05,
                                "cumulative_return_pct": 0.00,
                                "trade_count": candidate_trade_count,
                            },
                        ],
                    }
                )
        return runs


if __name__ == "__main__":
    unittest.main()
