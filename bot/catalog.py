"""What the API's models can take (images, video) and cost — read from the public /models list."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .net import http_session

log = logging.getLogger(__name__)

REFRESH_SECONDS = 6 * 3600
# A typical short account breakdown: 5 videos x 6 frames + transcripts in, the short report (with reasoning) out.
TYPICAL_INPUT_TOKENS, TYPICAL_OUTPUT_TOKENS = 20_000, 6_000
# Whole videos instead of frames: ~290 tokens per second of video with sound, 5 clips of ~30 s.
VIDEO_EXTRA_TOKENS = 5 * 30 * 290

# Shown as buttons in /model: (model id, short note).
SUGGESTED = (
    ("openai/gpt-6.1-terra", "сбалансированная"),
    ("google/gemini-3.8-flash", "смотрит видео целиком"),
    ("moonshotai/kimi-k2.6", "сильная, недорогая"),
    ("anthropic/claude-haiku-5.5", "самая дешёвая"),
    ("anthropic/claude-sonnet-5.5", "глубже, дороже"),
)


@dataclass
class ModelInfo:
    id: str
    input_modalities: list[str] = field(default_factory=list)
    prompt_rub_per_m: float | None = None
    completion_rub_per_m: float | None = None

    @property
    def images(self) -> bool:
        return "image" in self.input_modalities

    @property
    def video(self) -> bool:
        return "video" in self.input_modalities

    def estimate_rub(self, with_video: bool = False) -> float | None:
        if self.prompt_rub_per_m is None or self.completion_rub_per_m is None:
            return None
        tokens_in = TYPICAL_INPUT_TOKENS + (VIDEO_EXTRA_TOKENS if with_video else 0)
        return (self.prompt_rub_per_m * tokens_in + self.completion_rub_per_m * TYPICAL_OUTPUT_TOKENS) / 1e6


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_models(data: dict[str, Any]) -> dict[str, ModelInfo]:
    models = {}
    for item in data.get("data") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        provider = item.get("top_provider") or {}
        pricing = provider.get("pricing") or item.get("pricing") or {}
        models[item["id"]] = ModelInfo(
            id=item["id"],
            input_modalities=list((item.get("architecture") or {}).get("input_modalities") or []),
            prompt_rub_per_m=_float(pricing.get("prompt_per_million")),
            completion_rub_per_m=_float(pricing.get("completion_per_million")),
        )
    return models


class ModelCatalog:
    def __init__(self, base_url: str):
        self.url = base_url.rstrip("/") + "/models"
        self._models: dict[str, ModelInfo] = {}
        self._fetched_at = 0.0

    @property
    def loaded(self) -> bool:
        return bool(self._models)

    async def get(self, model_id: str) -> ModelInfo | None:
        """None when the model is unknown or the list could not be loaded (callers then just try the model)."""
        if time.time() - self._fetched_at > REFRESH_SECONDS:
            try:
                async with http_session(None, timeout=20) as session:
                    async with session.get(self.url) as response:
                        response.raise_for_status()
                        self._models = parse_models(await response.json(content_type=None))
                self._fetched_at = time.time()
            except Exception as exc:  # noqa: BLE001 - the catalog only refines behaviour
                log.warning("could not load the model list from %s: %s", self.url, exc)
        return self._models.get(model_id)
