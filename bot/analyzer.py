"""Explaining why videos went viral, with Claude."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, ValidationError

from .media import VideoMedia
from .metrics import ProfileStats
from .models import PLATFORM_LABELS, Video

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
Ты — продюсер коротких вертикальных видео (TikTok, Instagram Reels, YouTube Shorts) и аналитик контента \
в нише нейросетей и ИИ-инструментов. Тебе дают данные роликов: метрики, подпись, хэштеги, музыку, \
расшифровку речи и кадры (первые секунды и дальше по ролику). Твоя задача — объяснить, почему ролики \
залетели, и дать конкретные улучшения автору, который снимает про нейросети.

Как работать:
- Опирайся на данные. Цитируй фразы из расшифровки, описывай, что видно на кадрах: текст на экране, лицо \
в кадре, интерфейс нейросети, смену планов. Если данных нет (нет расшифровки или кадров), так и скажи и \
не придумывай содержание ролика.
- Сравнивай хиты с медианой аккаунта и со слабыми роликами: что есть у залетевших и чего нет у остальных.
- Разбирай хук (первые 1–3 секунды), обещание и ценность для зрителя, структуру и удержание, подачу, \
оформление, призыв к действию, тему и её актуальность, формат (туториал, обзор сервиса, «до/после», \
новости ИИ, разоблачение, подборка), триггеры для комментариев, сохранений и репостов. Сохранения и \
репосты относительно просмотров — сильный сигнал для алгоритма.
- Советы должны быть конкретными и применимыми к следующему ролику: что сказать в первые 2 секунды, \
какой текст поставить на экран, что вырезать, какую тему взять. Никаких общих фраз вроде «делайте \
качественный контент».
- Пиши по-русски, простым языком, без markdown-разметки (без звёздочек и решёток).\
"""


class VideoAnalysis(BaseModel):
    video_number: int
    why_it_worked: str
    hook: str
    structure: str
    what_to_improve: list[str]
    repeat_this: list[str]


class ProfileReport(BaseModel):
    summary: str
    hit_patterns: list[str]
    videos: list[VideoAnalysis]
    weak_spots: list[str]
    recommendations: list[str]
    content_ideas: list[str]


class SingleReport(BaseModel):
    video: VideoAnalysis
    how_to_adapt: list[str]
    content_ideas: list[str]


def _strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema adjusted for structured outputs (no extra keys, everything required)."""
    schema = model.model_json_schema()

    def fix(node: dict) -> None:
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        node.pop("title", None)
        for child in node.get("properties", {}).values():
            fix(child)
        if isinstance(node.get("items"), dict):
            fix(node["items"])
        for child in node.get("$defs", {}).values():
            fix(child)

    fix(schema)
    return schema


def _fmt(n: int | float | None) -> str:
    if n is None:
        return "нет данных"
    return f"{int(n):,}".replace(",", " ")


def describe_video(video: Video, number: int | None = None, ratio: float | None = None, media: VideoMedia | None = None) -> str:
    lines = []
    title = f"Ролик №{number}" if number is not None else "Ролик"
    lines.append(f"=== {title} ({PLATFORM_LABELS.get(video.platform, video.platform)}, @{video.author}) ===")
    lines.append(f"Ссылка: {video.url}")
    if video.created_at:
        lines.append(f"Опубликован: {video.created_at:%Y-%m-%d} ({video.age_days():.0f} дн. назад)")
    duration = (media.duration if media and media.duration else None) or video.duration
    if duration:
        lines.append(f"Длительность: {duration:.0f} с")
    lines.append(
        f"Просмотры: {_fmt(video.views)}; лайки: {_fmt(video.likes)}; комментарии: {_fmt(video.comments)}; "
        f"репосты: {_fmt(video.shares)}; сохранения: {_fmt(video.saves)}"
    )
    if video.engagement_rate is not None:
        lines.append(f"Вовлечённость (ER): {video.engagement_rate * 100:.1f}%")
    if ratio:
        lines.append(f"Просмотры относительно медианы аккаунта: ×{ratio:.1f}")
    if video.author_followers:
        lines.append(f"Подписчиков у автора: {_fmt(video.author_followers)}")
    if video.is_pinned:
        lines.append("Закреплён в профиле")
    lines.append(f"Подпись: {video.caption.strip() or '(пусто)'}")
    if video.hashtags:
        lines.append("Хэштеги: " + " ".join("#" + t for t in video.hashtags))
    if video.music:
        lines.append(f"Звук: {video.music}")
    if media is not None:
        if media.transcript:
            lines.append(f"Расшифровка речи ({media.transcript_source}): {media.transcript[:6000]}")
        else:
            lines.append("Расшифровки речи нет.")
        if media.error:
            lines.append(f"Видео не обработано: {media.error}")
    return "\n".join(lines)


def _video_blocks(video: Video, number: int, ratio: float | None, media: VideoMedia | None) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": describe_video(video, number, ratio, media)}]
    if media and media.frames:
        seconds = ", ".join(f"{s:g} с" for s, _ in media.frames)
        blocks.append({"type": "text", "text": f"Кадры ролика №{number} на секундах: {seconds}"})
        for _, jpeg in media.frames:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(jpeg).decode()},
            })
    return blocks


class AnalysisError(RuntimeError):
    pass


@dataclass
class Analyzer:
    model: str
    effort: str
    api_key: str | None = None

    def __post_init__(self) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key) if self.api_key else anthropic.AsyncAnthropic()

    async def profile_report(
        self, platform: str, username: str, stats: ProfileStats, media: dict[str, VideoMedia]
    ) -> ProfileReport:
        intro = (
            f"Аккаунт @{username} в {PLATFORM_LABELS.get(platform, platform)}. Проанализировано последних роликов: "
            f"{len(stats.videos)}. Медиана просмотров: {_fmt(stats.median_views)}."
        )
        if stats.mean_er is not None:
            intro += f" Средний ER: {stats.mean_er * 100:.1f}%."
        if stats.posts_per_week:
            intro += f" Частота публикаций: ~{stats.posts_per_week:.1f} в неделю."
        content: list[dict] = [{"type": "text", "text": intro + "\n\nЗАЛЕТЕВШИЕ РОЛИКИ (подробно, с кадрами):"}]
        for i, video in enumerate(stats.hits, 1):
            content += _video_blocks(video, i, stats.ratio(video), media.get(video.key))

        if stats.flops:
            flops = "\n\n".join(describe_video(v, ratio=stats.ratio(v)) for v in stats.flops)
            content.append({"type": "text", "text": "СЛАБЫЕ РОЛИКИ ДЛЯ СРАВНЕНИЯ (только метаданные):\n\n" + flops})

        content.append({"type": "text", "text": (
            "Сделай разбор аккаунта. В videos дай отдельный разбор каждого залетевшего ролика (video_number — его номер). "
            "summary — 3–5 предложений о том, что работает на этом аккаунте. hit_patterns — общие черты хитов. "
            "weak_spots — что отличает слабые ролики. recommendations — 5–8 конкретных шагов. "
            "content_ideas — 5 идей следующих роликов про нейросети в формате «хук — о чём ролик»."
        )})
        return await self._ask(content, ProfileReport)

    async def single_report(self, video: Video, media: VideoMedia | None) -> SingleReport:
        content = _video_blocks(video, 1, None, media)
        content.append({"type": "text", "text": (
            "Разбери этот ролик: почему он набрал такие показатели, как устроен хук и структура, что можно улучшить. "
            "how_to_adapt — как повторить этот приём на своём канале про нейросети, не копируя ролик. "
            "content_ideas — 3 идеи роликов по мотивам в формате «хук — о чём ролик»."
        )})
        return await self._ask(content, SingleReport)

    async def _ask(self, content: list[dict], model: type[BaseModel]):
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": _strict_schema(model)},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise AnalysisError("Неверный ANTHROPIC_API_KEY") from exc
        except anthropic.RateLimitError as exc:
            raise AnalysisError("Превышен лимит запросов к Claude, попробуйте через минуту") from exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"Ошибка Claude API ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError("Не удалось связаться с Claude API") from exc

        log.info("Claude usage: %s", message.usage)
        if message.stop_reason == "refusal":
            raise AnalysisError("Claude отказался разбирать этот контент")
        if message.stop_reason == "max_tokens":
            raise AnalysisError("Ответ Claude обрезан по лимиту токенов — уменьшите ANALYZE_TOP")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return model.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.error("Bad analysis JSON: %s\n%s", exc, text[:2000])
            raise AnalysisError("Claude вернул ответ в неожиданном формате") from exc
