"""Explaining why videos went viral, with an LLM (Claude via Polza.ai by default)."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from . import costs
from .llm import LLMClient, LLMError
from .media import VideoMedia
from .metrics import ProfileStats
from .models import PLATFORM_LABELS, Video

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — продюсер коротких вертикальных видео (TikTok, Reels, Shorts) в нише нейросетей и ИИ-инструментов. \
Тебе дают ролики: метрики, подпись, хэштеги, звук, расшифровку речи и кадры. Объясни автору, почему ролики \
залетели и как ему сделать похожие.

Об авторе: своё лицо он не снимает. Он может записать экран компьютера или телефона, снять подсъём без лица \
(руки, клавиатура, телефон в руке, предметы, улица) и сделать остальное нейросетями: голос, ИИ-аватар, \
генерация видео и картинок, субтитры. Для каждого ролика дай рецепт, как собрать похожий в таком формате: \
что на экране, чем озвучить, какими сервисами сделать (например, ElevenLabs для голоса, HeyGen для аватара, \
Kling, Runway или Veo для видео, Midjourney для картинок, CapCut для монтажа и субтитров; можно другие). \
Если в оригинале держит лицо автора, предложи, чем его заменить без потери хука.

Правила ответа:
- Только главное: без воды, вступлений и пересказа метрик. Каждый пункт — одно короткое предложение, до 20 слов.
- Опирайся на данные: что видно на кадрах, какие фразы звучат. Если данных нет, не выдумывай содержание ролика.
- Хук — что зритель видит и слышит в первые 2 секунды и почему не листает дальше.
- Никаких общих советов вроде «делайте качественный контент» — только конкретика к следующему ролику.
- Пиши по-русски, без markdown-разметки.\
"""


class VideoAnalysis(BaseModel):
    video_number: int
    why: str
    hook: str
    how_to_make: list[str]
    improve: list[str]


class ProfileReport(BaseModel):
    summary: str
    takeaways: list[str]
    videos: list[VideoAnalysis]
    ideas: list[str]


class SingleReport(BaseModel):
    video: VideoAnalysis
    ideas: list[str]


VIDEO_TEMPLATE = """{
      "video_number": 1,
      "why": "1–2 предложения: главная причина, почему ролик залетел",
      "hook": "1 предложение: что в первые 2 секунды и почему цепляет",
      "how_to_make": ["что на экране и чем это снять или сгенерировать", "чем озвучить", "монтаж, текст на экране, субтитры", "3–5 коротких шагов, без лица автора"],
      "improve": ["что улучшить — не больше 2 пунктов"]
    }"""

PROFILE_TEMPLATE = """{
  "summary": "2–3 предложения: что работает на этом аккаунте",
  "takeaways": ["главный вывод или что делать — ровно 3 пункта"],
  "videos": [
    %s
  ],
  "ideas": ["хук — о чём ролик — как сделать без лица; ровно 3 идеи"]
}""" % VIDEO_TEMPLATE

SINGLE_TEMPLATE = """{
  "video": %s,
  "ideas": ["хук — о чём ролик — как сделать без лица; ровно 3 идеи"]
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


def _video_blocks(
    video: Video, number: int, ratio: float | None, media: VideoMedia | None, use_video: bool = False
) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": describe_video(video, number, ratio, media)}]
    if use_video and media and media.clip:
        blocks.append({"type": "text", "text": f"Ролик №{number} целиком, со звуком:"})
        data_url = "data:video/mp4;base64," + base64.standard_b64encode(media.clip).decode()
        blocks.append({"type": "video_url", "video_url": {"url": data_url}})
    elif media and media.frames:
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


# The API rejects a request it cannot take (e.g. video for a model or route without video) with these.
VIDEO_REJECTED_STATUSES = {400, 413, 415, 422}


@dataclass
class Analyzer:
    llm: LLMClient
    reasoning_effort: str | None = None
    # Models that rejected whole videos in this run of the bot: skip straight to frames for them.
    video_rejected: dict[str, str] = field(default_factory=dict)

    async def profile_report(
        self, platform: str, username: str, stats: ProfileStats, media: dict[str, VideoMedia],
        *, model: str, use_video: bool = False,
    ) -> ProfileReport:
        intro = (
            f"Аккаунт @{username} в {PLATFORM_LABELS.get(platform, platform)}. Проанализировано последних роликов: "
            f"{len(stats.videos)}. Медиана просмотров: {_fmt(stats.median_views)}."
        )
        if stats.mean_er is not None:
            intro += f" Средний ER: {stats.mean_er * 100:.1f}%."
        if stats.posts_per_week:
            intro += f" Частота публикаций: ~{stats.posts_per_week:.1f} в неделю."

        def build(with_video: bool) -> list[dict]:
            kind = "целиком, со звуком" if with_video else "с кадрами"
            content: list[dict] = [{"type": "text", "text": f"{intro}\n\nЗАЛЕТЕВШИЕ РОЛИКИ (подробно, {kind}):"}]
            for i, video in enumerate(stats.hits, 1):
                content += _video_blocks(video, i, stats.ratio(video), media.get(video.key), with_video)
            if stats.flops:
                flops = "\n\n".join(describe_video(v, ratio=stats.ratio(v)) for v in stats.flops)
                content.append({"type": "text", "text": "СЛАБЫЕ РОЛИКИ ДЛЯ СРАВНЕНИЯ (только метаданные):\n\n" + flops})
            content.append({"type": "text", "text": (
                "Сделай короткий разбор аккаунта. В videos — по одному объекту на каждый залетевший ролик "
                "(video_number — его номер). Сравни хиты со слабыми роликами, но в ответ пиши только главные выводы."
            )})
            return content

        has_clips = any(m.clip for m in media.values())
        return await self._ask_media(build, ProfileReport, PROFILE_TEMPLATE, model, use_video and has_clips)

    async def single_report(
        self, video: Video, media: VideoMedia | None, *, model: str, use_video: bool = False
    ) -> SingleReport:
        def build(with_video: bool) -> list[dict]:
            content = _video_blocks(video, 1, None, media, with_video)
            content.append({"type": "text", "text": (
                "Коротко разбери этот ролик: почему он набрал такие показатели и как сделать похожий без лица. "
                "ideas — идеи роликов по мотивам, не копии этого."
            )})
            return content

        has_clip = bool(media and media.clip)
        return await self._ask_media(build, SingleReport, SINGLE_TEMPLATE, model, use_video and has_clip)

    async def _ask_media(self, build, report_model: type[BaseModel], template: str, model: str, use_video: bool):
        """Send whole videos when asked; if the API refuses them, redo the request with frames and say so."""
        if use_video and model in self.video_rejected:
            costs.add_note(f"🎞 {model} не принял видео ({self.video_rejected[model]}) — разбор по кадрам")
            use_video = False
        if not use_video:
            return await self._ask(build(False), report_model, template, model)
        try:
            report = await self._ask(build(True), report_model, template, model)
        except AnalysisError as exc:
            cause = exc.__cause__
            if not isinstance(cause, LLMError) or cause.status not in VIDEO_REJECTED_STATUSES:
                raise
            log.warning("%s rejected video input, falling back to frames: %s", model, cause)
            self.video_rejected[model] = str(cause)[:150]
            costs.add_note(f"🎞 {model} не принял видео ({self.video_rejected[model]}) — разбор по кадрам")
            return await self._ask(build(False), report_model, template, model)
        costs.add_note("🎞 нейросеть смотрела ролики целиком, со звуком")
        return report

    async def _ask(self, content: list[dict], report_model: type[BaseModel], template: str, model: str):
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [*content, {"type": "text", "text": JSON_INSTRUCTION + template}]},
        ]
        payload = {"model": model, "max_tokens": 32000, "messages": messages}
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
                return report_model.model_validate(extract_json(text))
            except (ValueError, ValidationError) as exc:
                log.warning("Bad analysis JSON (attempt %d): %s\n%s", attempt + 1, exc, text[:2000])
                # Ask once more, showing the model its own answer and what was wrong with it.
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Ответ не разобрался как JSON нужной структуры ({str(exc)[:300]}). "
                                                "Пришли исправленный JSON целиком, без пояснений."},
                ]
        raise AnalysisError("Нейросеть вернула ответ в неожиданном формате — попробуйте ещё раз или другую модель (/model)")
