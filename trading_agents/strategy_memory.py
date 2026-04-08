from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_agents.reporting import LOCAL_TZ


def current_strategy_slot(now: datetime | None = None) -> str:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else datetime.now(LOCAL_TZ)
    slot = "day" if local_now.hour >= 12 else "night"
    return f"{local_now.strftime('%Y-%m-%d')}-{slot}"


def load_strategy_memory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "slot": "",
            "updated_at": "",
            "summary": "",
            "biases": [],
            "risk_adjustments": [],
            "focus_symbols": [],
        }
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {
            "slot": "",
            "updated_at": "",
            "summary": "",
            "biases": [],
            "risk_adjustments": [],
            "focus_symbols": [],
        }
    if not isinstance(payload, dict):
        return {
            "slot": "",
            "updated_at": "",
            "summary": "",
            "biases": [],
            "risk_adjustments": [],
            "focus_symbols": [],
        }
    payload.setdefault("slot", "")
    payload.setdefault("updated_at", "")
    payload.setdefault("summary", "")
    payload.setdefault("biases", [])
    payload.setdefault("risk_adjustments", [])
    payload.setdefault("focus_symbols", [])
    return payload


def save_strategy_memory(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
