from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Taipei")
REPORT_WINDOW_ANCHOR_HOUR_LOCAL = 12


def _trace_date_label(now: datetime | None = None) -> str:
    local_now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    if local_now.hour < REPORT_WINDOW_ANCHOR_HOUR_LOCAL:
        local_now = local_now - timedelta(days=1)
    return local_now.strftime("%Y-%m-%d")


def _safe_slug(value: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    normalized = normalized.strip("-")
    return normalized or "unknown"


def _append_agent_trace(trace_root: Path, model: str, prompt: str, trace: dict, response: object, *, status: str) -> None:
    timestamp = datetime.now(timezone.utc)
    date_label = str(trace.get("date_label") or _trace_date_label(timestamp))
    agent = _safe_slug(str(trace.get("agent") or "unknown"))
    trace_dir = trace_root / date_label
    trace_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = trace_dir / f"{agent}.jsonl"
    md_path = trace_dir / f"{agent}.md"
    payload = {
        "timestamp_utc": timestamp.isoformat(),
        "timestamp_local": timestamp.astimezone(LOCAL_TZ).isoformat(),
        "agent": str(trace.get("agent") or "unknown"),
        "stage": str(trace.get("stage") or ""),
        "symbol": str(trace.get("symbol") or ""),
        "timeframe": str(trace.get("timeframe") or ""),
        "date_label": date_label,
        "model": model,
        "status": status,
        "trace": trace,
        "prompt": prompt,
        "response": response,
    }
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    context_bits = []
    for key in ("symbol", "stage", "timeframe", "slot", "window", "selected_strategy_id", "cycle_mode"):
        value = trace.get(key)
        if value not in {None, ""}:
            context_bits.append(f"- {key}: {value}")
    context_section = "\n".join(context_bits) if context_bits else "- context: n/a"
    response_block = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, indent=2)
    with md_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    f"## {payload['timestamp_local']}",
                    "",
                    f"- model: {model}",
                    f"- status: {status}",
                    context_section,
                    "",
                    "### Prompt",
                    "",
                    "```text",
                    prompt,
                    "```",
                    "",
                    "### Response",
                    "",
                    "```json",
                    response_block,
                    "```",
                    "",
                ]
            )
        )


@dataclass(frozen=True)
class OllamaClient:
    host: str
    model: str
    timeout_seconds: float = 60.0
    trace_root: Path | None = None

    def generate_json(self, prompt: str, trace: dict | None = None) -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        request = Request(
            f"{self.host.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.timeout_seconds, 1.0)) as response:
                body = json.loads(response.read().decode("utf-8"))
            raw = body.get("response", "{}")
            parsed = json.loads(raw)
            if self.trace_root is not None and isinstance(trace, dict):
                _append_agent_trace(self.trace_root, self.model, prompt, trace, parsed, status="ok")
            return parsed
        except Exception as exc:
            if self.trace_root is not None and isinstance(trace, dict):
                _append_agent_trace(
                    self.trace_root,
                    self.model,
                    prompt,
                    trace,
                    {"error": str(exc)},
                    status="error",
                )
            raise
