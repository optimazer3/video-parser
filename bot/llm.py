"""A small client for an OpenAI-compatible API (Polza.ai by default): chat and speech-to-text."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiohttp

from . import costs

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}
ATTEMPTS = 3


class LLMError(RuntimeError):
    pass


def _error_text(status: int, body: Any) -> str:
    message = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        message = message or (error if isinstance(error, str) else None) or body.get("message")
    elif isinstance(body, str):
        message = body[:300]
    if status == 401:
        return "Неверный POLZA_API_KEY"
    if status == 402:
        return "Недостаточно средств на балансе Polza"
    return f"Ошибка API нейросети ({status}): {message or 'без описания'}"


class LLMClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(1, ATTEMPTS + 1):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.post(f"{self.base_url}{path}", json=payload, headers=headers) as response:
                        try:
                            body: Any = await response.json(content_type=None)
                        except ValueError:
                            body = await response.text()
                        if response.status < 400 and isinstance(body, dict):
                            return body
                        if response.status < 400:
                            raise LLMError(f"Неожиданный ответ API: {str(body)[:300]}")
                        if response.status not in RETRY_STATUSES or attempt == ATTEMPTS:
                            raise LLMError(_error_text(response.status, body))
                        log.warning("%s -> %s, retry %d", path, response.status, attempt)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if attempt == ATTEMPTS:
                    raise LLMError(f"Не удалось связаться с API нейросети: {exc or type(exc).__name__}") from exc
                log.warning("%s connection error %r, retry %d", path, exc, attempt)
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError("unreachable")

    async def chat(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        """Returns (text, finish_reason)."""
        data = await self._post("/chat/completions", payload, timeout=600)
        usage = data.get("usage") or {}
        log.info("LLM %s usage: %s", payload.get("model"), usage)
        costs.add_llm(data.get("model") or payload.get("model"), usage.get("cost_rub"))
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Пустой ответ нейросети: {str(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content and message.get("refusal"):
            raise LLMError(f"Нейросеть отказалась отвечать: {message['refusal']}")
        return content or "", choices[0].get("finish_reason")

    async def transcribe(self, audio: bytes, model: str, mime: str = "audio/mp3") -> str | None:
        payload = {
            "model": model,
            "file": f"data:{mime};base64,{base64.b64encode(audio).decode()}",
            "response_format": "json",
        }
        data = await self._post("/audio/transcriptions", payload, timeout=300)
        costs.add_llm(model, (data.get("usage") or {}).get("cost_rub"))
        text = (data.get("text") or "").strip()
        return text or None
