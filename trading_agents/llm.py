from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class OllamaClient:
    host: str
    model: str
    timeout_seconds: float = 60.0

    def generate_json(self, prompt: str) -> dict:
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
        with urlopen(request, timeout=max(self.timeout_seconds, 1.0)) as response:
            body = json.loads(response.read().decode("utf-8"))
        raw = body.get("response", "{}")
        return json.loads(raw)
