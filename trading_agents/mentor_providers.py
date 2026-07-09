from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import error, request


def generate_json(system_prompt: str, user_prompt: str, timeout: float) -> dict[str, Any]:
    provider = os.getenv("EXTERNAL_MENTOR_PROVIDER", "")
    providers = [item.strip().lower() for item in os.getenv("EXTERNAL_MENTOR_PROVIDERS", "openai,gemini").split(",") if item.strip()]
    selected = (provider or (providers[0] if providers else "openai")).strip().lower()
    model = _model_for_provider(selected)
    return generate_json_for_provider(
        provider=selected,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout=timeout,
    )


def generate_json_for_provider(
    *,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
) -> dict[str, Any]:
    provider_key = str(provider or "").strip().lower()
    model_name = str(model or _model_for_provider(provider_key)).strip()
    try:
        if provider_key == "openai":
            payload = _call_openai(model=model_name, system_prompt=system_prompt, user_prompt=user_prompt, timeout=timeout)
        elif provider_key == "gemini":
            payload = _call_gemini(model=model_name, system_prompt=system_prompt, user_prompt=user_prompt, timeout=timeout)
        else:
            raise RuntimeError(f"unsupported mentor provider: {provider_key}")
        return {"provider": provider_key, "model": model_name, "status": "ok", "payload": payload}
    except Exception as exc:
        return {"provider": provider_key, "model": model_name, "status": "error", "error": str(exc)}


def generate_all_json(
    *,
    providers: list[str] | tuple[str, ...],
    models: dict[str, str],
    system_prompt: str,
    user_prompt: str,
    timeout: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for provider in providers:
        provider_key = str(provider or "").strip().lower()
        if not provider_key:
            continue
        results.append(
            generate_json_for_provider(
                provider=provider_key,
                model=models.get(provider_key, _model_for_provider(provider_key)),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout=timeout,
            )
        )
    return results


def _model_for_provider(provider: str) -> str:
    provider_key = str(provider or "").strip().lower()
    if provider_key == "gemini":
        return os.getenv("EXTERNAL_MENTOR_GEMINI_MODEL", "gemini-2.5-flash")
    if provider_key == "openai":
        return os.getenv("EXTERNAL_MENTOR_OPENAI_MODEL", "gpt-5.5")
    return ""


def _call_openai(*, model: str, system_prompt: str, user_prompt: str, timeout: float) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing OPENAI_API_KEY")
    if not model:
        raise RuntimeError("missing OpenAI mentor model")
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    raw = _post_json(
        "https://api.openai.com/v1/responses",
        body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    text = _extract_openai_text(raw).strip()
    if not text:
        raise RuntimeError("OpenAI returned no JSON text")
    return _parse_json_text(text)


def _call_gemini(*, model: str, system_prompt: str, user_prompt: str, timeout: float) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing GEMINI_API_KEY")
    if not model:
        raise RuntimeError("missing Gemini mentor model")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    raw = _post_json(endpoint, body, headers={"Content-Type": "application/json"}, timeout=timeout)
    text = _extract_gemini_text(raw).strip()
    if not text:
        raise RuntimeError("Gemini returned no JSON text")
    return _parse_json_text(text)


def _post_json(url: str, body: dict[str, Any], *, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    retryable_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code not in retryable_statuses or attempt >= 3:
                raise last_error from exc
        except error.URLError as exc:
            last_error = RuntimeError(f"network error: {exc}")
            if attempt >= 3:
                raise last_error from exc
        time.sleep(2 ** (attempt - 1))
    raise last_error or RuntimeError("provider request failed")


def _extract_openai_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    fragments: list[str] = []
    for item in payload.get("output", []) or []:
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                fragments.append(part["text"])
    return "\n".join(fragments)


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    for candidate in payload.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


def _parse_json_text(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("provider JSON root must be an object")
    return payload
