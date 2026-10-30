"""Explaining why videos went viral, with an LLM (Claude via Polza.ai by default)."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from .llm import LLMClient, LLMError
from .media import VideoMedia
from .metrics import ProfileStats
from .models import PLATFORM_LABELS, Video

log = logging.getLogger(__name__)

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


VIDEO_TEMPLATE = """{
      "video_number": 1,
      "why_it_worked": "почему ролик залетел, с опорой на кадры, речь и метрики",
      "hook": "что происходит в первые 1–3 секунды и почему это цепляет",
      "structure": "структура, подача, удержание, оформление, призыв к действию",
      "what_to_improve": ["конкретное улучшение", "..."],
      "repeat_this": ["приём, который стоит повторять", "..."]
    }"""

PROFILE_TEMPLATE = """{
  "summary": "3–5 предложений: что работает на этом аккаунте",
  "hit_patterns": ["общая черта залетевших роликов", "..."],
  "videos": [
    %s
  ],
  "weak_spots": ["что отличает слабые ролики", "..."],
  "recommendations": ["конкретный шаг", "..."],
  "content_ideas": ["хук — о чём ролик", "..."]
}""" % VIDEO_TEMPLATE

SINGLE_TEMPLATE = """{
  "video": %s,
  "how_to_adapt": ["как применить приём на своём канале", "..."],
  "content_ideas": ["хук — о чём ролик", "..."]
}""" % VIDEO_TEMPLATE

JSON_INSTRUCTION = (
    "Ответь только одним JSON-объектом — без пояснений до или после и без ```. "
    "Строго такая структура (значения — по-русски, ключи — как в образце):\n"
)


def extract_json(text: str) -> dict:
    """Take the JSON object out of a reply, tolerating ```json fences and stray text around it."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    return json.loads(text[start : end + 1])


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
        if media.cover_only:
            label = f"Видео ролика №{number} скачать не удалось, ниже только его обложка."
        else:
            label = f"Кадры ролика №{number} на секундах: " + ", ".join(f"{s:g} с" for s, _ in media.frames)
        blocks.append({"type": "text", "text": label})
        for _, jpeg in media.frames:
            data_url = "data:image/jpeg;base64," + base64.standard_b64encode(jpeg).decode()
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return blocks


class AnalysisError(RuntimeError):
    pass


@dataclass
class Analyzer:
    llm: LLMClient
    model: str
    reasoning_effort: str | None = None

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
        return await self._ask(content, ProfileReport, PROFILE_TEMPLATE)

    async def single_report(self, video: Video, media: VideoMedia | None) -> SingleReport:
        content = _video_blocks(video, 1, None, media)
        content.append({"type": "text", "text": (
            "Разбери этот ролик: почему он набрал такие показатели, как устроен хук и структура, что можно улучшить. "
            "how_to_adapt — как повторить этот приём на своём канале про нейросети, не копируя ролик. "
            "content_ideas — 3 идеи роликов по мотивам в формате «хук — о чём ролик»."
        )})
        return await self._ask(content, SingleReport, SINGLE_TEMPLATE)

    async def _ask(self, content: list[dict], model: type[BaseModel], template: str):
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [*content, {"type": "text", "text": JSON_INSTRUCTION + template}]},
        ]
        payload = {"model": self.model, "max_tokens": 32000, "messages": messages}
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}

        for attempt in range(2):
            try:
                text, finish_reason = await self.llm.chat(payload)
            except LLMError as exc:
                raise AnalysisError(str(exc)) from exc
            if finish_reason == "length":
                raise AnalysisError("Ответ нейросети обрезан по лимиту токенов — уменьшите ANALYZE_TOP")
            if finish_reason == "content_filter":
                raise AnalysisError("Нейросеть отказалась разбирать этот контент")
            try:
                return model.model_validate(extract_json(text))
            except (ValueError, ValidationError) as exc:
                log.warning("Bad analysis JSON (attempt %d): %s\n%s", attempt + 1, exc, text[:2000])
                # Ask once more, showing the model its own answer and what was wrong with it.
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Ответ не разобрался как JSON нужной структуры ({str(exc)[:300]}). "
                                                "Пришли исправленный JSON целиком, без пояснений."},
                ]
        raise AnalysisError("Нейросеть вернула ответ в неожиданном формате — попробуйте ещё раз или другую LLM_MODEL")
