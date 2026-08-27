from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Taipei")


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
    normalized.setdefault("promotion_plan", {})
    if not isinstance(normalized.get("controls"), dict):
        normalized["controls"] = {}
    experiment = normalized.get("experiment")
    if not isinstance(experiment, dict):
        normalized["experiment"] = {}
    elif experiment and not experiment_is_active(experiment, current_slot=current_slot):
        normalized["experiment"] = {}
    if not isinstance(normalized.get("promotion_plan"), dict):
        normalized["promotion_plan"] = {}
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
