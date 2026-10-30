"""The bot's main workflows, independent from Telegram."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

from .analyzer import Analyzer, ProfileReport, SingleReport
from .apify_source import ApifyError, ApifySource
from .config import Settings
from .llm import LLMClient
from .media import MediaProcessor, VideoMedia
from .metrics import ProfileStats, pick_feed, profile_stats
from .models import INSTAGRAM, PLATFORM_LABELS, TIKTOK, Video
from .storage import Storage

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    return None


@dataclass
class FeedResult:
    videos: list[Video]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProfileResult:
    stats: ProfileStats
    report: ProfileReport | None


class Services:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.apify = ApifySource(settings)
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key) if settings.llm_api_key else None
        self.media = MediaProcessor(
            settings.apify_token, settings.frames_per_video, llm, settings.transcribe_model, settings.media_proxy
        )
        self.analyzer = Analyzer(llm, settings.llm_model, settings.llm_reasoning_effort) if llm else None
        self._user_locks: dict[int, asyncio.Lock] = {}

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def feed(self, user_id: int, progress: Progress = _noop) -> FeedResult:
        s = self.settings
        topics = await self.storage.get_topics(user_id, s.default_topics)
        await progress(f"🔎 Ищу свежие ролики по темам: {', '.join(topics)}")

        jobs = {}
        if TIKTOK in s.feed_platforms:
            jobs[TIKTOK] = self.apify.tiktok_topics(topics, s.feed_results_per_topic)
        if INSTAGRAM in s.feed_platforms:
            jobs[INSTAGRAM] = self.apify.instagram_topics(topics, s.feed_results_per_topic)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)

        videos: list[Video] = []
        warnings: list[str] = []
        for platform, result in zip(jobs, results):
            if isinstance(result, Exception):
                log.exception("feed %s failed", platform, exc_info=result)
                warnings.append(f"{PLATFORM_LABELS[platform]}: {result}")
            else:
                videos += result

        picked = pick_feed(
            videos,
            seen=await self.storage.seen_keys(user_id),
            days=s.feed_days,
            min_views=s.feed_min_views,
            min_likes=s.feed_min_likes,
            limit=s.feed_size,
        )
        await self.storage.save_videos(picked)
        return FeedResult(picked, warnings)

    async def analyze_profile(self, platform: str, username: str, progress: Progress = _noop) -> ProfileResult:
        s = self.settings
        label = PLATFORM_LABELS[platform]
        await progress(f"⏳ Собираю последние {s.profile_videos} роликов @{username} ({label})…")
        videos = await self.apify.profile(platform, username, s.profile_videos)
        if not videos:
            raise ApifyError(f"Не нашёл роликов у @{username} — проверьте ник и что аккаунт открытый")

        stats = profile_stats(videos, top=s.analyze_top)
        await self.storage.save_videos(stats.hits)
        if self.analyzer is None:
            return ProfileResult(stats, None)

        await progress(f"🎬 Скачиваю {len(stats.hits)} лучших роликов, вырезаю кадры и расшифровываю речь…")
        hits = await self.apify.with_media(stats.hits)
        media = await self._process_media(hits)

        await progress("🧠 Разбираю, почему ролики залетели… (1–3 минуты)")
        report = await self.analyzer.profile_report(platform, username, stats, media)
        return ProfileResult(stats, report)

    async def analyze_video(
        self, video: Video | None = None, *, platform: str | None = None, url: str | None = None,
        progress: Progress = _noop,
    ) -> tuple[Video, SingleReport]:
        if video is None:
            await progress("⏳ Загружаю ролик…")
            video = await self.apify.single_video(platform, url)
            if video is None:
                raise ApifyError("Не удалось получить ролик — проверьте ссылку")
            await self.storage.save_videos([video])
        elif video.platform == INSTAGRAM:
            # Instagram file links expire within hours, so refresh a cached one.
            fresh = await self.apify.single_video(INSTAGRAM, video.url)
            video = fresh or video
        await progress("🎬 Скачиваю ролик, вырезаю кадры и расшифровываю речь…")
        (video,) = await self.apify.with_media([video])
        media = await self._process_media([video])
        await progress("🧠 Разбираю ролик…")
        report = await self.analyzer.single_report(video, media.get(video.key))
        return video, report

    async def _process_media(self, videos: list[Video]) -> dict[str, VideoMedia]:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(*(self.media.process(v, session) for v in videos))
        return {v.key: m for v, m in zip(videos, results)}
