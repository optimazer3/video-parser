"""The bot's main workflows, independent from Telegram."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import costs
from .analyzer import Analyzer, ProfileReport, SingleReport
from .formatting import plural
from .apify_source import ApifyError, ApifySource, feed_cost_usd
from .catalog import ModelCatalog
from .config import Settings
from .llm import LLMClient
from .media import MediaProcessor, VideoMedia, short_error, ytdlp_info
from .metrics import ProfileStats, pick_feed, profile_stats
from .net import http_session
from .models import INSTAGRAM, PLATFORM_LABELS, TIKTOK, Video, tiktok_from_ytdlp
from .storage import Storage

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    return None


@dataclass
class FeedResult:
    videos: list[Video]
    warnings: list[str] = field(default_factory=list)
    checked: int = 0  # videos collected (and paid for) on Apify
    fresh: int = 0  # of them, published within FEED_DAYS


@dataclass
class ProfileResult:
    stats: ProfileStats
    report: ProfileReport | None
    media: dict[str, VideoMedia] = field(default_factory=dict)
    model: str | None = None


class Services:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.apify = ApifySource(settings)
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key) if settings.llm_api_key else None
        self.media = MediaProcessor(
            settings.apify_token, settings.frames_per_video, llm, settings.transcribe_model, settings.proxy
        )
        self.analyzer = Analyzer(llm, settings.llm_reasoning_effort) if llm else None
        self.catalog = ModelCatalog(settings.llm_base_url)
        self._user_locks: dict[int, asyncio.Lock] = {}

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def model_for(self, user_id: int) -> str:
        """The model picked with /model, otherwise LLM_MODEL from .env."""
        return await self.storage.get_setting(user_id, "model") or self.settings.llm_model

    async def video_enabled(self, user_id: int) -> bool:
        value = await self.storage.get_setting(user_id, "video")
        return value == "on" if value else self.settings.llm_video

    async def _llm_choice(self, user_id: int) -> tuple[str, bool]:
        """(model, send whole videos?) — whole videos only when switched on and the model accepts video."""
        model = await self.model_for(user_id)
        if not await self.video_enabled(user_id):
            return model, False
        info = await self.catalog.get(model)
        return model, bool(info and info.video)

    async def feed(self, user_id: int, progress: Progress = _noop) -> FeedResult:
        s = self.settings
        topics = await self.storage.get_topics(user_id, s.default_topics)
        platforms = [p for p in (TIKTOK, INSTAGRAM) if p in s.feed_platforms]
        estimate = feed_cost_usd(topics, s.feed_results_per_topic, platforms)
        await progress(
            f"🔎 Ищу ролики: {plural(len(topics), 'тема', 'темы', 'тем')} × {s.feed_results_per_topic} шт. в "
            f"{' и '.join(PLATFORM_LABELS[p] for p in platforms)} (Apify ≈ ${estimate:.2f})\n"
            f"Темы: {', '.join(topics)}"
        )

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
        fresh = sum(1 for v in videos if (v.age_days() or 0) <= s.feed_days)
        return FeedResult(picked, warnings, checked=len(videos), fresh=fresh)

    async def analyze_profile(
        self, user_id: int, platform: str, username: str, progress: Progress = _noop
    ) -> ProfileResult:
        s = self.settings
        label = PLATFORM_LABELS[platform]
        cached = await self.storage.get_profile(platform, username, s.profile_cache_hours)
        if cached:
            videos, popular = cached
            costs.mark_cached()
        else:
            videos, popular = await self._scrape_profile(platform, username, progress)

        stats = profile_stats(videos, top=s.analyze_top, popular=popular)
        await self.storage.save_videos(stats.hits)
        if self.analyzer is None:
            return ProfileResult(stats, None)

        await progress(f"🎬 Скачиваю {len(stats.hits)} лучших роликов, вырезаю кадры и расшифровываю речь…")
        model, use_video = await self._llm_choice(user_id)
        media = await self._process_media(stats.hits, make_clip=use_video)

        await progress("🧠 Разбираю, почему ролики залетели… (1–3 минуты)")
        report = await self.analyzer.profile_report(
            platform, username, stats, media, model=model, use_video=use_video
        )
        return ProfileResult(stats, report, media, model)

    async def _scrape_profile(
        self, platform: str, username: str, progress: Progress
    ) -> tuple[list[Video], list[Video]]:
        s = self.settings
        extra = f" и {s.profile_popular} самых популярных" if platform == TIKTOK and s.profile_popular > 0 else ""
        await progress(f"⏳ Собираю последние {s.profile_videos}{extra} роликов @{username} ({PLATFORM_LABELS[platform]})…")
        latest, popular = await asyncio.gather(
            self.apify.profile(platform, username, s.profile_videos),
            self.apify.profile_popular(platform, username, s.profile_popular),
            return_exceptions=True,
        )
        if isinstance(latest, Exception):
            raise latest
        if isinstance(popular, Exception):
            log.warning("popular videos of %s failed: %s", username, popular)
            costs.add_note(f"Топ роликов за всё время не собрался ({str(popular)[:100]}) — разбор только по последним")
            popular = []
        if not latest:
            raise ApifyError(f"Не нашёл роликов у @{username} — проверьте ник и что аккаунт открытый")
        if len(latest) < max(5, s.profile_videos // 2):
            # TikTok sometimes gives Apify only part of a profile; such a result must not stick in the cache.
            costs.add_note(f"⚠️ Apify вернул только {len(latest)} из {s.profile_videos} последних роликов — "
                           "выбор хитов может быть неточным, попробуйте ещё раз позже")
        else:
            await self.storage.save_profile(platform, username, latest, popular)
        return latest, popular

    async def analyze_video(
        self, user_id: int, video: Video | None = None, *, platform: str | None = None, url: str | None = None,
        progress: Progress = _noop,
    ) -> tuple[Video, SingleReport]:
        if video is None:
            await progress("⏳ Загружаю ролик…")
            video = await self._tiktok_by_ytdlp(url) if platform == TIKTOK else None
            video = video or await self.apify.single_video(platform, url)
            if video is None:
                raise ApifyError("Не удалось получить ролик — проверьте ссылку")
            await self.storage.save_videos([video])
        elif video.platform == INSTAGRAM:
            # Instagram file links expire within hours, so refresh a cached one.
            fresh = await self.apify.single_video(INSTAGRAM, video.url)
            video = fresh or video
        await progress("🎬 Скачиваю ролик, вырезаю кадры и расшифровываю речь…")
        model, use_video = await self._llm_choice(user_id)
        media = await self._process_media([video], make_clip=use_video)
        await progress("🧠 Разбираю ролик…")
        report = await self.analyzer.single_report(video, media.get(video.key), model=model, use_video=use_video)
        return video, report

    async def _tiktok_by_ytdlp(self, url: str) -> Video | None:
        """TikTok stats for a single link straight from TikTok via yt-dlp — no paid Apify run."""
        try:
            return tiktok_from_ytdlp(await asyncio.to_thread(ytdlp_info, url, self.settings.proxy))
        except Exception as exc:  # noqa: BLE001 - fall back to Apify
            log.warning("yt-dlp info failed for %s: %s", url, short_error(exc))
            return None

    async def _process_media(self, videos: list[Video], make_clip: bool = False) -> dict[str, VideoMedia]:
        media = await self._download_media(videos, make_clip)
        # yt-dlp is free but can break when TikTok changes something; then pay Apify ~$0.005 per video.
        failed = [v for v in videos if v.platform == TIKTOK and not v.video_url and media[v.key].error]
        if failed:
            log.info("yt-dlp failed for %d TikTok videos, downloading them through Apify", len(failed))
            try:
                stored = {v.id: v for v in await self.apify.tiktok_media([v.url for v in failed])}
            except ApifyError as exc:
                log.warning("Apify download failed: %s", exc)
                return media
            retry = []
            for video in failed:
                if stored.get(video.id) and stored[video.id].video_url:
                    video.video_url = stored[video.id].video_url
                    retry.append(video)
            media.update(await self._download_media(retry, make_clip))
        return media

    async def _download_media(self, videos: list[Video], make_clip: bool = False) -> dict[str, VideoMedia]:
        if not videos:
            return {}
        async with http_session(self.settings.proxy, timeout=180) as session:
            results = await asyncio.gather(*(self.media.process(v, session, make_clip) for v in videos))
        return {v.key: m for v, m in zip(videos, results)}
