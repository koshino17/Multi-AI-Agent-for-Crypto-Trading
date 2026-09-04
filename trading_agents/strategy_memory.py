from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Taipei")
_BENCHMARK_WATCH_ALIASES = {
    "bollinger_keltner_extversion_v1": "bollinger_keltner_extreme_reversion_v1",
}


def current_strategy_slot(now: datetime | None = None) -> str:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else datetime.now(LOCAL_TZ)
    slot = "day" if local_now.hour >= 12 else "night"
    return f"{local_now.strftime('%Y-%m-%d')}-{slot}"


def strategy_slot_index(slot: str) -> int | None:
    raw = str(slot or "").strip()
    if not raw:
        return None
    try:
        date_label, half = raw.rsplit("-", 1)
        base = datetime.strptime(date_label, "%Y-%m-%d").date().toordinal() * 2
    except ValueError:
        return None
    if half == "night":
        return base
    if half == "day":
        return base + 1
    return None


def experiment_is_active(experiment: dict[str, Any] | None, *, current_slot: str | None = None) -> bool:
    if not isinstance(experiment, dict) or not experiment:
        return False
    ttl_windows = int(experiment.get("ttl_windows", 0) or 0)
    if ttl_windows <= 0:
        return False
    experiment_slot = str(experiment.get("slot", "") or "").strip()
    active_slot = str(current_slot or current_strategy_slot()).strip()
    experiment_index = strategy_slot_index(experiment_slot)
    current_index = strategy_slot_index(active_slot)
    if experiment_index is None or current_index is None:
        return str(experiment.get("status", "") or "").strip().lower() == "active"
    return (current_index - experiment_index) < ttl_windows


@lru_cache(maxsize=1)
def _known_benchmark_candidate_ids() -> frozenset[str]:
    config_path = Path(__file__).resolve().parent.parent / "config" / "external_benchmark_library.json"
    try:
        payload = json.loads(config_path.read_text())
    except Exception:
        return frozenset()
    strategies = payload.get("strategies") if isinstance(payload, dict) else []
    ids = {
        str(item.get("id", "")).strip()
        for item in strategies
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    return frozenset(ids)


def normalize_benchmark_watch_candidate(value: Any) -> str:
    candidate_id = str(value or "").strip()
    if not candidate_id:
        return ""
    candidate_id = _BENCHMARK_WATCH_ALIASES.get(candidate_id, candidate_id)
    known_ids = _known_benchmark_candidate_ids()
    if known_ids and candidate_id not in known_ids:
        return ""
    return candidate_id


def normalize_strategy_memory_payload(payload: dict[str, Any] | None, *, current_slot: str | None = None) -> dict[str, Any]:
    normalized = payload if isinstance(payload, dict) else {}
    normalized.setdefault("slot", "")
    normalized.setdefault("updated_at", "")
    normalized.setdefault("summary", "")
    normalized.setdefault("biases", [])
    normalized.setdefault("risk_adjustments", [])
    normalized.setdefault("focus_symbols", [])
    normalized.setdefault("controls", {})
    normalized.setdefault("experiment", {})
    if not isinstance(normalized.get("controls"), dict):
        normalized["controls"] = {}
    controls = normalized.get("controls") or {}
    benchmark_watch_candidate = normalize_benchmark_watch_candidate(controls.get("benchmark_watch_candidate"))
    if benchmark_watch_candidate:
        controls["benchmark_watch_candidate"] = benchmark_watch_candidate
    else:
        controls.pop("benchmark_watch_candidate", None)
    experiment = normalized.get("experiment")
    if not isinstance(experiment, dict):
        normalized["experiment"] = {}
    elif experiment and not experiment_is_active(experiment, current_slot=current_slot):
        normalized["experiment"] = {}
    elif experiment:
        control_deltas = experiment.get("control_deltas")
        if isinstance(control_deltas, dict):
            delta = control_deltas.get("benchmark_watch_candidate")
            if isinstance(delta, dict):
                current_value = normalize_benchmark_watch_candidate(delta.get("current"))
                previous_value = normalize_benchmark_watch_candidate(delta.get("previous"))
                if current_value:
                    delta["current"] = current_value
                else:
                    delta.pop("current", None)
                if previous_value:
                    delta["previous"] = previous_value
                else:
                    delta.pop("previous", None)
    return normalized


def load_strategy_memory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return normalize_strategy_memory_payload({})
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return normalize_strategy_memory_payload({})
    return normalize_strategy_memory_payload(payload)


def save_strategy_memory(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
