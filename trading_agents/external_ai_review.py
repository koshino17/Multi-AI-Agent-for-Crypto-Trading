from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


def external_ai_review_path(storage, date_label: str) -> Path:
    return storage.service / f"external_ai_review-{date_label}.json"


def load_external_ai_review(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_external_ai_review(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def generate_external_ai_review(
    *,
    date_label: str,
    daily_summary: dict[str, Any],
    daily_review: dict[str, Any],
    settings,
) -> dict[str, Any]:
    if not getattr(settings, "external_ai_review_enabled", False):
        return {"status": "disabled", "reason": "external AI review disabled"}
    provider = str(getattr(settings, "external_ai_review_provider", "gemini") or "gemini").strip().lower()
    api_key = str(getattr(settings, "external_ai_review_api_key", "") or "").strip()
    model = str(getattr(settings, "external_ai_review_model", "") or "").strip()
    timeout_seconds = float(getattr(settings, "external_ai_review_timeout_seconds", 20.0) or 20.0)
    if not api_key:
        return {"status": "disabled", "reason": "missing external AI review API key"}
    if provider != "gemini":
        return {"status": "disabled", "reason": f"unsupported external AI provider: {provider}"}
    if not model:
        return {"status": "disabled", "reason": "missing external AI review model"}
    try:
        payload = _generate_gemini_review(
            model=model,
            api_key=api_key,
            date_label=date_label,
            daily_summary=daily_summary,
            daily_review=daily_review,
            timeout_seconds=timeout_seconds,
        )
        payload["status"] = "updated"
        payload["provider"] = provider
        payload["model"] = model
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return payload
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "provider": provider, "model": model}


def _generate_gemini_review(
    *,
    model: str,
    api_key: str,
    date_label: str,
    daily_summary: dict[str, Any],
    daily_review: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    prompt = _build_external_review_prompt(date_label=date_label, daily_summary=daily_summary, daily_review=daily_review)
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You are an external quantitative trading reviewer. "
                        "Review the daily report objectively. "
                        "Do not praise by default. "
                        "Return strict JSON with keys summary, strengths, concerns, action_items, verdict. "
                        "strengths/concerns/action_items must be arrays of short strings. "
                        "verdict must be one of: improve, hold, strong_day, weak_day."
                    )
                }
            ]
        },
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        f"{endpoint}?key={api_key}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {exc.code}: {detail}") from exc
    parsed = json.loads(raw)
    text = _extract_gemini_text(parsed).strip()
    if not text:
        raise RuntimeError("Gemini API returned no review text")
    review = json.loads(text)
    return {
        "summary": str(review.get("summary", "")).strip(),
        "strengths": [str(item).strip() for item in review.get("strengths", []) if str(item).strip()][:5],
        "concerns": [str(item).strip() for item in review.get("concerns", []) if str(item).strip()][:5],
        "action_items": [str(item).strip() for item in review.get("action_items", []) if str(item).strip()][:5],
        "verdict": str(review.get("verdict", "")).strip().lower(),
        "raw_text": text,
    }


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    for candidate in payload.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


def _build_external_review_prompt(*, date_label: str, daily_summary: dict[str, Any], daily_review: dict[str, Any]) -> str:
    financial = daily_summary.get("financial_snapshot", {})
    symbol_postmortem = daily_summary.get("symbol_postmortem") or {}
    loss_attribution = daily_summary.get("loss_attribution") or {}
    trade_review = daily_summary.get("trade_review") or {}
    external_benchmarks = daily_summary.get("external_benchmarks") or {}
    top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
    benchmark_for_symbol = loss_attribution.get("focus_symbol_benchmark") or {}
    payload = {
        "date": date_label,
        "mode": daily_summary.get("mode"),
        "totals": {
            "decisions": daily_summary.get("total"),
            "proposals": daily_summary.get("proposals"),
            "approved": daily_summary.get("approved"),
            "submitted_orders": daily_summary.get("submitted_orders"),
            "executed_trades": daily_summary.get("executed"),
            "blocked": daily_summary.get("blocked"),
            "rejected_orders": daily_summary.get("rejected_orders"),
        },
        "financial": {
            "configured_initial_usdt": financial.get("initial_capital_usdt"),
            "day_start_portfolio_value_usdt": financial.get("day_start_portfolio_value_usdt"),
            "total_portfolio_value_usdt": financial.get("total_portfolio_value_usdt"),
            "daily_pnl_usdt": financial.get("daily_pnl_usdt"),
            "daily_pnl_pct": financial.get("daily_pnl_pct"),
            "realized_pnl_usdt": financial.get("realized_pnl_usdt"),
            "unrealized_pnl_usdt": financial.get("unrealized_pnl_usdt"),
            "daily_fees_usdt": financial.get("daily_fees_usdt"),
            "available_usdt": financial.get("available_usdt"),
            "gross_exposure_pct": financial.get("gross_exposure_pct"),
            "effective_leverage": financial.get("effective_leverage"),
        },
        "decision_attribution": daily_summary.get("decision_source_counts"),
        "accepted_attribution": daily_summary.get("accepted_source_counts"),
        "blocked_reason_counts": daily_summary.get("blocked_reason_counts"),
        "rejection_reason_counts": daily_summary.get("rejection_reason_counts"),
        "llm_wake_rate_pct": daily_summary.get("llm_wake_rate_pct"),
        "avg_decision_latency_seconds": daily_summary.get("avg_decision_latency_seconds"),
        "latest_decision": {
            "symbol": (daily_summary.get("latest") or {}).get("selected_symbol"),
            "action": ((daily_summary.get("latest") or {}).get("idea") or {}).get("action"),
            "score": ((daily_summary.get("latest") or {}).get("idea") or {}).get("score"),
            "decision_source": (daily_summary.get("latest") or {}).get("decision_source"),
        },
        "trade_review": {
            "summary": trade_review.get("summary"),
            "long_episodes": trade_review.get("long_episodes"),
            "short_episodes": trade_review.get("short_episodes"),
            "closed_winners": trade_review.get("closed_winners"),
            "closed_losers": trade_review.get("closed_losers"),
        },
        "loss_attribution": {
            "primary_driver": loss_attribution.get("primary_driver"),
            "accepted_source_counts": loss_attribution.get("accepted_source_counts"),
            "losing_episodes_by_source": loss_attribution.get("losing_episodes_by_source"),
            "observations": loss_attribution.get("observations"),
        },
        "symbol_postmortem": {
            "symbol": symbol_postmortem.get("symbol"),
            "summary": symbol_postmortem.get("summary"),
            "improvements": symbol_postmortem.get("improvement_directions"),
        },
        "daily_review": {
            "strategist_review": daily_review.get("strategist_review"),
            "risk_review": daily_review.get("risk_review"),
            "benchmark_review": daily_review.get("benchmark_review"),
            "execution_review": daily_review.get("execution_review"),
            "consensus_summary": daily_review.get("consensus_summary"),
            "action_items": daily_review.get("action_items"),
        },
        "benchmarks": {
            "top_candidate": top_benchmark,
            "focus_symbol_benchmark": benchmark_for_symbol,
        },
    }
    return (
        "Review the following TradePulse daily snapshot. "
        "Focus on what actually mattered today, not generic advice. "
        "If the report is incomplete or noisy, say so. "
        "Return JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
