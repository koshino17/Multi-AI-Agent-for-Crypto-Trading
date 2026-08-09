from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trading_agents.mentor_providers import generate_all_json
from trading_agents.strategy_memory import load_strategy_memory, save_strategy_memory
from trading_agents.strategy_research import run_strategy_tournament


MENTOR_ROLES = ("strategist", "risk_supervisor", "executor", "strategy_reflector")
PROMPT_PATCH_OPS = {"add_rule", "replace_rule", "remove_rule"}
LIVE_CONTROL_WHITELIST = {
    "benchmark_watch_candidate",
    "benchmark_watch_symbol",
    "entry_mode",
    "pilot_candidate_id",
    "pilot_max_position_pct",
}
ALLOWED_ENTRY_MODES = {"normal", "capital_preservation_pilot"}


def mentor_review_path(storage, date_label: str) -> Path:
    return storage.service / f"mentor_review-{date_label}.json"


def shadow_prompt_path(storage, date_label: str) -> Path:
    return storage.service / f"shadow_prompt-{date_label}.json"


def shadow_gate_path(storage, date_label: str) -> Path:
    return storage.service / f"shadow_gate-{date_label}.json"


def mentor_promotion_history_path(storage) -> Path:
    return storage.service / "mentor_promotion_history.jsonl"


def run_mentor_cycle(
    *,
    date_label: str,
    daily_summary: dict[str, Any],
    daily_review: dict[str, Any],
    settings,
    storage,
    mode: str = "",
    promote: bool = True,
    provider_runner: Callable[..., list[dict[str, Any]]] | None = None,
    benchmark_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    storage.service.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    evidence = build_mentor_evidence_bundle(daily_summary=daily_summary, daily_review=daily_review)
    prompts = {role: build_role_prompt(role=role, date_label=date_label, evidence=evidence) for role in MENTOR_ROLES}
    shadow_prompt_payload = {
        "date_label": date_label,
        "generated_at": generated_at,
        "mode": mode or daily_summary.get("mode", ""),
        "roles": list(MENTOR_ROLES),
        "schema": mentor_role_schema(),
        "evidence": evidence,
        "prompts": prompts,
    }
    _write_json(shadow_prompt_path(storage, date_label), shadow_prompt_payload)

    providers = list(getattr(settings, "external_mentor_providers", ("openai", "gemini")) or ())
    models = {
        "openai": str(getattr(settings, "external_mentor_openai_model", "gpt-5.5") or "gpt-5.5"),
        "gemini": str(getattr(settings, "external_mentor_gemini_model", "gemini-2.5-flash") or "gemini-2.5-flash"),
    }
    timeout = float(getattr(settings, "mentor_timeout_seconds", 30.0) or 30.0)
    role_results: dict[str, list[dict[str, Any]]] = {}
    for role, prompt_payload in prompts.items():
        if provider_runner is not None:
            results = provider_runner(role=role, system_prompt=prompt_payload["system"], user_prompt=prompt_payload["user"], timeout=timeout)
        else:
            results = generate_all_json(
                providers=providers,
                models=models,
                system_prompt=prompt_payload["system"],
                user_prompt=prompt_payload["user"],
                timeout=timeout,
            )
        role_results[role] = [_normalize_provider_result(item) for item in results]

    consensus = merge_provider_consensus(role_results, expected_providers=providers)
    candidate_id = _candidate_from_consensus(consensus)
    focus_symbol = _focus_symbol(daily_summary, settings)
    validation_symbols = _validation_symbols(settings)
    gate = evaluate_shadow_gate(
        settings=settings,
        storage=storage,
        candidate_id=candidate_id,
        focus_symbol=focus_symbol,
        validation_symbols=validation_symbols,
        benchmark_runs=benchmark_runs,
    )
    _write_json(shadow_gate_path(storage, date_label), gate)

    autopromote_allowed = bool(consensus.get("provider_health", {}).get("all_required_ok")) and bool(gate.get("pass"))
    if not bool(getattr(settings, "mentor_autopromote_enabled", True)):
        autopromote_allowed = False
    if not promote:
        autopromote_allowed = False

    promotion = apply_autopromotion(
        date_label=date_label,
        storage=storage,
        consensus=consensus,
        gate=gate,
        autopromote_allowed=autopromote_allowed,
        promote=promote,
    )
    review_payload = {
        "date_label": date_label,
        "generated_at": generated_at,
        "mode": mode or daily_summary.get("mode", ""),
        "status": "updated",
        "providers": providers,
        "roles": role_results,
        "role_summaries": _role_summaries(role_results),
        "consensus": consensus,
        "gate": gate,
        "promotion": promotion,
        "artifacts": {
            "mentor_review": str(mentor_review_path(storage, date_label)),
            "shadow_prompt": str(shadow_prompt_path(storage, date_label)),
            "shadow_gate": str(shadow_gate_path(storage, date_label)),
            "promotion_history": str(mentor_promotion_history_path(storage)),
        },
    }
    _write_json(mentor_review_path(storage, date_label), review_payload)
    mentor_promotion_history_path(storage).touch(exist_ok=True)
    return {
        "status": "updated",
        "date_label": date_label,
        "autopromote_allowed": autopromote_allowed,
        "gate_pass": bool(gate.get("pass")),
        "gate_reasons": gate.get("reasons", []),
        "promoted_keys": promotion.get("promoted_keys", []),
        "artifacts": review_payload["artifacts"],
        "provider_health": consensus.get("provider_health", {}),
    }


def build_mentor_evidence_bundle(*, daily_summary: dict[str, Any], daily_review: dict[str, Any]) -> dict[str, Any]:
    worst_episodes, worst_orders = extract_worst_trade_evidence(daily_summary.get("trade_review") or {})
    financial = daily_summary.get("financial_snapshot") or {}
    return {
        "daily_summary": {
            "date_label": daily_summary.get("date_label"),
            "mode": daily_summary.get("mode"),
            "total": daily_summary.get("total"),
            "proposals": daily_summary.get("proposals"),
            "approved": daily_summary.get("approved"),
            "submitted_orders": daily_summary.get("submitted_orders"),
            "executed": daily_summary.get("executed"),
            "blocked": daily_summary.get("blocked"),
            "rejected_orders": daily_summary.get("rejected_orders"),
            "financial_snapshot": {
                "total_portfolio_value_usdt": financial.get("total_portfolio_value_usdt"),
                "daily_pnl_usdt": financial.get("daily_pnl_usdt"),
                "daily_pnl_pct": financial.get("daily_pnl_pct"),
                "realized_pnl_usdt": financial.get("realized_pnl_usdt"),
                "daily_fees_usdt": financial.get("daily_fees_usdt"),
                "gross_exposure_pct": financial.get("gross_exposure_pct"),
                "effective_leverage": financial.get("effective_leverage"),
            },
            "decision_source_counts": daily_summary.get("decision_source_counts"),
            "accepted_source_counts": daily_summary.get("accepted_source_counts"),
            "blocked_reason_counts": daily_summary.get("blocked_reason_counts"),
            "rejection_reason_counts": daily_summary.get("rejection_reason_counts"),
            "po3_phase_performance": daily_summary.get("po3_phase_performance"),
            "shadow_benchmark_watch": daily_summary.get("shadow_benchmark_watch"),
            "strategy_research_latest": daily_summary.get("strategy_research_latest"),
        },
        "daily_review": daily_review,
        "worst_episodes_top3": worst_episodes,
        "worst_orders_top3": worst_orders,
    }


def extract_worst_trade_evidence(trade_review: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    episodes = [item for item in trade_review.get("episodes", []) or [] if isinstance(item, dict)]
    closed = [item for item in episodes if _episode_is_closed(item)]
    closed.sort(key=lambda item: _safe_float(item.get("estimated_edge_pct"), default=0.0))
    worst = [_episode_evidence(item) for item in closed[:3]]
    orders = [_order_evidence(item) for item in closed[:3]]
    return worst, orders


def build_role_prompt(*, role: str, date_label: str, evidence: dict[str, Any]) -> dict[str, str]:
    role_focus = {
        "strategist": "Find strategy and market-structure changes that improve expectancy after costs.",
        "risk_supervisor": "Find risk, sizing, drawdown, and promotion controls that protect capital.",
        "executor": "Find execution, order, latency, and fee changes that improve realized fills.",
        "strategy_reflector": "Convert the evidence into durable prompt rules and testable hypotheses.",
    }.get(role, "Review TradePulse evidence.")
    system_prompt = (
        "You are an asynchronous TradePulse external mentor. "
        "You never participate in intraday live decisions. "
        "Return strict JSON only. "
        f"Role: {role}. {role_focus} "
        "Allowed prompt_patch_structured operations are add_rule, replace_rule, remove_rule. "
        "Do not invent live controls outside the provided schema."
    )
    user_prompt = (
        "Review this daily TradePulse evidence bundle and return one JSON object with exactly these keys: "
        "summary, findings, controls_patch, prompt_patch_structured, benchmark_hypothesis, confidence. "
        "findings must be an array of short strings. "
        "controls_patch must be a JSON object. "
        "prompt_patch_structured must be an array of objects using only op add_rule, replace_rule, remove_rule. "
        "confidence must be a number from 0 to 1. "
        f"Date: {date_label}\n\n"
        + json.dumps(evidence, ensure_ascii=False)
    )
    return {"system": system_prompt, "user": user_prompt}


def mentor_role_schema() -> dict[str, Any]:
    return {
        "summary": "string",
        "findings": ["string"],
        "controls_patch": "object",
        "prompt_patch_structured": [{"op": "add_rule|replace_rule|remove_rule", "target": "string", "value": "any"}],
        "benchmark_hypothesis": "object|string",
        "confidence": "number",
    }


def merge_provider_consensus(
    role_results: dict[str, list[dict[str, Any]]],
    *,
    expected_providers: list[str] | tuple[str, ...] = ("openai", "gemini"),
) -> dict[str, Any]:
    expected = {str(item).strip().lower() for item in expected_providers if str(item).strip()}
    ok_provider_names = {
        str(result.get("provider", "")).strip().lower()
        for results in role_results.values()
        for result in results
        if result.get("status") == "ok" and isinstance(result.get("payload"), dict)
    }
    failed = [
        {
            "role": role,
            "provider": result.get("provider"),
            "model": result.get("model"),
            "error": result.get("error", "unknown"),
        }
        for role, results in role_results.items()
        for result in results
        if result.get("status") != "ok"
    ]
    provider_health = {
        "expected": sorted(expected),
        "ok": sorted(ok_provider_names),
        "failed": failed,
        "all_required_ok": bool(expected) and expected.issubset(ok_provider_names) and not failed,
    }
    safe_patch: dict[str, Any] = {"controls_patch": {}, "prompt_patch_structured": {}, "benchmark_hypothesis": {}}
    conflict_patch: dict[str, Any] = {"controls_patch": {}, "prompt_patch_structured": {}, "benchmark_hypothesis": {}}
    for role, results in role_results.items():
        payloads = [_normalize_role_payload(result.get("payload")) for result in results if result.get("status") == "ok"]
        if len(payloads) < 2:
            if payloads:
                conflict_patch["controls_patch"][role] = {
                    "reason": "single_provider_only",
                    "value": payloads[0].get("controls_patch", {}),
                }
            continue
        _merge_consensus_object(
            safe_patch["controls_patch"],
            conflict_patch["controls_patch"],
            role,
            [payload.get("controls_patch", {}) for payload in payloads],
        )
        _merge_consensus_value(
            safe_patch["prompt_patch_structured"],
            conflict_patch["prompt_patch_structured"],
            role,
            [payload.get("prompt_patch_structured", []) for payload in payloads],
        )
        _merge_consensus_value(
            safe_patch["benchmark_hypothesis"],
            conflict_patch["benchmark_hypothesis"],
            role,
            [payload.get("benchmark_hypothesis", {}) for payload in payloads],
        )
    return {"safe_patch": safe_patch, "conflict_patch": conflict_patch, "provider_health": provider_health}


def evaluate_shadow_gate(
    *,
    settings,
    storage,
    candidate_id: str,
    focus_symbol: str,
    validation_symbols: tuple[str, ...],
    benchmark_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    windows = _gate_windows(settings)
    min_trades = int(getattr(settings, "mentor_gate_min_trades", 8) or 8)
    if not candidate_id:
        return _gate_result(False, ["missing candidate_id"], candidate_id, focus_symbol, windows, [])
    runs = benchmark_runs if benchmark_runs is not None else _run_gate_tournaments(settings, storage, focus_symbol, validation_symbols, windows)
    comparisons: list[dict[str, Any]] = []
    reasons: list[str] = []
    focus_key = _symbol_key(focus_symbol)
    for window in windows:
        comparison = _comparison_for(runs, focus_key, int(window), candidate_id)
        comparisons.append(comparison)
        if not comparison.get("available"):
            reasons.append(f"{focus_symbol} window {window}: missing baseline or candidate")
            continue
        candidate = comparison["candidate"]
        if bool(candidate.get("uses_custom_cost_model", False)):
            reasons.append(f"{focus_symbol} window {window}: candidate uses research-only custom cost model")
        if int(candidate.get("trade_count", 0) or 0) < min_trades:
            reasons.append(f"{focus_symbol} window {window}: candidate trade_count below {min_trades}")
        if int(window) == 96:
            if float(comparison.get("expectancy_delta", 0.0)) < float(getattr(settings, "mentor_gate_min_expectancy_delta_96", 0.03)):
                reasons.append(f"{focus_symbol} window 96: expectancy_delta below threshold")
            if float(comparison.get("profit_factor_delta", 0.0)) < float(getattr(settings, "mentor_gate_min_pf_delta_96", 0.0)):
                reasons.append(f"{focus_symbol} window 96: profit_factor_delta below threshold")
        else:
            if float(comparison.get("expectancy_delta", 0.0)) < float(getattr(settings, "mentor_gate_min_expectancy_delta_long", 0.0)):
                reasons.append(f"{focus_symbol} window {window}: expectancy_delta below long threshold")
            if float(comparison.get("profit_factor_delta", 0.0)) < float(getattr(settings, "mentor_gate_min_pf_delta_long", -0.05)):
                reasons.append(f"{focus_symbol} window {window}: profit_factor_delta below long threshold")
            baseline_return = _safe_float(comparison["baseline"].get("cumulative_return_pct"))
            candidate_return = _safe_float(candidate.get("cumulative_return_pct"))
            max_gap = float(getattr(settings, "mentor_gate_max_cum_return_gap_pct", 0.50) or 0.50)
            if candidate_return < baseline_return - max_gap:
                reasons.append(f"{focus_symbol} window {window}: cumulative_return_pct trails baseline by more than {max_gap:.2f}pp")
    validation_keys = {_symbol_key(item) for item in validation_symbols if item}
    validation_keys.update({"BTC/USDT", "ETH/USDT"})
    for symbol in sorted(validation_keys):
        for window in [item for item in windows if int(item) in {320, 1000}]:
            comparison = _comparison_for(runs, symbol, int(window), candidate_id)
            if not comparison.get("available"):
                reasons.append(f"{symbol} window {window}: missing validation candidate")
                comparisons.append(comparison)
                continue
            candidate = comparison["candidate"]
            if bool(candidate.get("uses_custom_cost_model", False)):
                reasons.append(f"{symbol} window {window}: validation candidate uses research-only custom cost model")
            if _safe_float(candidate.get("expectancy_pct")) <= -0.10:
                reasons.append(f"{symbol} window {window}: validation expectancy_pct <= -0.10")
            if _safe_float(candidate.get("profit_factor")) <= 0.80:
                reasons.append(f"{symbol} window {window}: validation profit_factor <= 0.80")
            comparisons.append({**comparison, "validation": True})
    return _gate_result(not reasons, reasons, candidate_id, focus_symbol, windows, comparisons)


def apply_autopromotion(
    *,
    date_label: str,
    storage,
    consensus: dict[str, Any],
    gate: dict[str, Any],
    autopromote_allowed: bool,
    promote: bool = True,
) -> dict[str, Any]:
    raw_controls = _flatten_role_controls((consensus.get("safe_patch") or {}).get("controls_patch") or {})
    live_controls = _whitelisted_live_controls(raw_controls)
    promotion = {
        "status": "shadow_only",
        "reason": "gate or provider health did not allow promotion",
        "promoted_keys": [],
        "live_controls_patch": live_controls,
        "shadow_only_controls": {key: value for key, value in raw_controls.items() if key not in live_controls},
    }
    history_path = mentor_promotion_history_path(storage)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(exist_ok=True)
    if not promote:
        promotion["reason"] = "promotion disabled by caller"
        return promotion
    if not autopromote_allowed:
        return promotion
    if not live_controls:
        promotion["reason"] = "no whitelisted live controls in safe consensus"
        return promotion
    memory = load_strategy_memory(storage.strategy_memory_state)
    controls = memory.setdefault("controls", {})
    controls.update(live_controls)
    memory["mentor_last_promotion"] = {
        "date": date_label,
        "candidate": gate.get("candidate_id", ""),
        "promoted_keys": sorted(live_controls),
        "gate_metrics": gate,
    }
    memory["mentor_shadow_reference"] = {
        "mentor_review": str(mentor_review_path(storage, date_label)),
        "shadow_prompt": str(shadow_prompt_path(storage, date_label)),
        "shadow_gate": str(shadow_gate_path(storage, date_label)),
    }
    save_strategy_memory(storage.strategy_memory_state, memory)
    history_row = {
        "date": date_label,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "candidate": gate.get("candidate_id", ""),
        "promoted_keys": sorted(live_controls),
        "live_controls_patch": live_controls,
        "gate_metrics": gate,
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, ensure_ascii=False) + "\n")
    return {
        **promotion,
        "status": "promoted",
        "reason": "gate passed and providers agreed",
        "promoted_keys": sorted(live_controls),
    }


def _episode_is_closed(item: dict[str, Any]) -> bool:
    status = str(item.get("status", "") or "").strip().lower()
    if status == "closed":
        return True
    return bool(item.get("closed_at") or item.get("closed_at_local") or item.get("closing_order") or item.get("close_price"))


def _episode_evidence(item: dict[str, Any]) -> dict[str, Any]:
    idea = item.get("idea") if isinstance(item.get("idea"), dict) else {}
    llm_wake = item.get("llm_wake") if isinstance(item.get("llm_wake"), dict) else {}
    return {
        "episode": item,
        "estimated_edge_pct": item.get("estimated_edge_pct"),
        "llm_wake": {"metrics": llm_wake.get("metrics")},
        "market_structure": item.get("market_structure"),
        "execution_constraints": item.get("execution_constraints"),
        "approval": item.get("approval"),
        "debate": item.get("debate"),
        "account": item.get("account"),
        "result": item.get("result"),
        "idea": {"rationale": idea.get("rationale")},
    }


def _order_evidence(item: dict[str, Any]) -> dict[str, Any]:
    order = item.get("closing_order") if isinstance(item.get("closing_order"), dict) else None
    source = "closing_order"
    if order is None:
        order = item.get("opening_order") if isinstance(item.get("opening_order"), dict) else None
        source = "opening_order"
    return {
        "episode_id": item.get("episode_id") or item.get("id"),
        "symbol": item.get("symbol"),
        "direction": item.get("direction"),
        "estimated_edge_pct": item.get("estimated_edge_pct"),
        "source": source if order is not None else "missing",
        "order": order,
    }


def _normalize_provider_result(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item or {})
    if result.get("status") == "ok":
        result["payload"] = _normalize_role_payload(result.get("payload"))
    return result


def _normalize_role_payload(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    controls_patch = data.get("controls_patch") if isinstance(data.get("controls_patch"), dict) else {}
    prompt_patch = data.get("prompt_patch_structured") if isinstance(data.get("prompt_patch_structured"), list) else []
    normalized_prompt = []
    for item in prompt_patch:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op", "")).strip()
        if op in PROMPT_PATCH_OPS:
            normalized_prompt.append(item)
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    return {
        "summary": str(data.get("summary", "") or "").strip(),
        "findings": [str(item).strip() for item in findings if str(item).strip()],
        "controls_patch": controls_patch,
        "prompt_patch_structured": normalized_prompt,
        "benchmark_hypothesis": data.get("benchmark_hypothesis") if data.get("benchmark_hypothesis") is not None else {},
        "confidence": _safe_float(data.get("confidence"), default=0.0),
    }


def _merge_consensus_object(safe: dict[str, Any], conflict: dict[str, Any], role: str, values: list[Any]) -> None:
    dicts = [value if isinstance(value, dict) else {} for value in values]
    keys = sorted({key for item in dicts for key in item})
    for key in keys:
        key_values = [item.get(key) for item in dicts if key in item]
        target = safe if _all_equal(key_values) and len(key_values) == len(dicts) else conflict
        target.setdefault(role, {})[key] = key_values[0] if target is safe else key_values


def _merge_consensus_value(safe: dict[str, Any], conflict: dict[str, Any], role: str, values: list[Any]) -> None:
    if _all_equal(values):
        safe[role] = values[0]
    else:
        conflict[role] = values


def _all_equal(values: list[Any]) -> bool:
    if not values:
        return False
    first = json.dumps(values[0], sort_keys=True, ensure_ascii=False)
    return all(json.dumps(value, sort_keys=True, ensure_ascii=False) == first for value in values[1:])


def _candidate_from_consensus(consensus: dict[str, Any]) -> str:
    controls = _flatten_role_controls((consensus.get("safe_patch") or {}).get("controls_patch") or {})
    for key in ("pilot_candidate_id", "benchmark_watch_candidate"):
        value = str(controls.get(key, "") or "").strip()
        if value:
            return value
    hypotheses = (consensus.get("safe_patch") or {}).get("benchmark_hypothesis") or {}
    for value in hypotheses.values() if isinstance(hypotheses, dict) else []:
        if isinstance(value, dict):
            candidate = str(value.get("candidate_id") or value.get("candidate") or "").strip()
            if candidate:
                return candidate
    return ""


def _focus_symbol(daily_summary: dict[str, Any], settings) -> str:
    for value in (
        daily_summary.get("symbol"),
        (daily_summary.get("symbol_postmortem") or {}).get("symbol"),
        (daily_summary.get("shadow_benchmark_watch") or {}).get("focus_symbol"),
    ):
        if str(value or "").strip():
            return str(value).strip()
    pool = list(getattr(settings, "observation_pool", ()) or ())
    return str(pool[0] if pool else getattr(settings, "symbol", "BTC/USDT") or "BTC/USDT")


def _validation_symbols(settings) -> tuple[str, ...]:
    raw = tuple(getattr(settings, "strategy_research_validation_symbols", ("BTC/USDT", "ETH/USDT")) or ())
    return raw or ("BTC/USDT", "ETH/USDT")


def _gate_windows(settings) -> tuple[int, ...]:
    raw = getattr(settings, "mentor_gate_windows", ("96", "320", "1000")) or ("96", "320", "1000")
    windows = []
    for item in raw:
        try:
            windows.append(int(item))
        except Exception:
            continue
    return tuple(windows or [96, 320, 1000])


def _run_gate_tournaments(settings, storage, focus_symbol: str, validation_symbols: tuple[str, ...], windows: tuple[int, ...]) -> list[dict[str, Any]]:
    symbols = [focus_symbol] + [item for item in validation_symbols if _symbol_key(item) != _symbol_key(focus_symbol)]
    runs: list[dict[str, Any]] = []
    for symbol in symbols:
        for window in windows:
            tournament = run_strategy_tournament(settings=settings, storage=storage, symbol=symbol, limit=int(window), include_alpha=False)
            payload = tournament.get("payload") or {}
            runs.append(
                {
                    "symbol": payload.get("symbol", symbol),
                    "window": int(window),
                    "baseline_strategy_id": payload.get("baseline_strategy_id", ""),
                    "ranked_results": payload.get("ranked_results", []),
                    "json_path": tournament.get("json_path"),
                }
            )
    return runs


def _comparison_for(runs: list[dict[str, Any]], symbol: str, window: int, candidate_id: str) -> dict[str, Any]:
    run = next(
        (
            item
            for item in runs
            if _symbol_key(item.get("symbol")) == _symbol_key(symbol)
            and int(item.get("window", item.get("limit", 0)) or 0) == int(window)
        ),
        {},
    )
    rows = [item for item in run.get("ranked_results", []) or [] if isinstance(item, dict)]
    baseline_id = str(run.get("baseline_strategy_id", "") or "").strip()
    baseline = next((item for item in rows if bool(item.get("baseline"))), None)
    if baseline is None and baseline_id:
        baseline = next((item for item in rows if str(item.get("candidate_id", "")) == baseline_id), None)
    candidate = next((item for item in rows if str(item.get("candidate_id", "")) == candidate_id), None)
    comparison = {
        "symbol": _symbol_key(symbol),
        "window": int(window),
        "candidate_id": candidate_id,
        "available": bool(baseline and candidate),
        "baseline": baseline or {},
        "candidate": candidate or {},
        "benchmark_json_path": run.get("json_path"),
    }
    if baseline and candidate:
        comparison["expectancy_delta"] = _safe_float(candidate.get("expectancy_pct")) - _safe_float(baseline.get("expectancy_pct"))
        comparison["profit_factor_delta"] = _safe_float(candidate.get("profit_factor")) - _safe_float(baseline.get("profit_factor"))
        comparison["cumulative_return_delta_pct"] = _safe_float(candidate.get("cumulative_return_pct")) - _safe_float(baseline.get("cumulative_return_pct"))
    return comparison


def _gate_result(pass_: bool, reasons: list[str], candidate_id: str, focus_symbol: str, windows: tuple[int, ...], comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "pass" if pass_ else "fail",
        "pass": bool(pass_),
        "candidate_id": candidate_id,
        "focus_symbol": focus_symbol,
        "windows": [int(item) for item in windows],
        "reasons": reasons if reasons else ["all mentor gate thresholds passed"],
        "comparisons": comparisons,
    }


def _flatten_role_controls(role_controls: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for value in role_controls.values():
        if isinstance(value, dict):
            flattened.update(value)
    return flattened


def _whitelisted_live_controls(raw_controls: dict[str, Any]) -> dict[str, Any]:
    live: dict[str, Any] = {}
    for key, value in raw_controls.items():
        if key not in LIVE_CONTROL_WHITELIST:
            continue
        if key == "entry_mode" and str(value) not in ALLOWED_ENTRY_MODES:
            continue
        live[key] = value
    return live


def _role_summaries(role_results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for role, results in role_results.items():
        summaries[role] = [
            {
                "provider": result.get("provider"),
                "model": result.get("model"),
                "status": result.get("status"),
                "summary": (result.get("payload") or {}).get("summary") if isinstance(result.get("payload"), dict) else "",
                "findings": ((result.get("payload") or {}).get("findings") or [])[:3] if isinstance(result.get("payload"), dict) else [],
                "error": result.get("error"),
            }
            for result in results
        ]
    return summaries


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _symbol_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "/" not in text and text.endswith("USDT"):
        return f"{text[:-4]}/USDT"
    return text


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
