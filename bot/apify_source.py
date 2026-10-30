"""Fetching videos through Apify actors."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClientAsync

from .config import Settings
from .models import INSTAGRAM, TIKTOK, Video, instagram_from_apify, tiktok_from_apify

log = logging.getLogger(__name__)

NO_DOWNLOADS = {
    "shouldDownloadVideos": False,
    "shouldDownloadCovers": False,
    "shouldDownloadSubtitles": False,
    "shouldDownloadSlideshowImages": False,
}


class ApifyError(RuntimeError):
    pass


def split_topics(topics: tuple[str, ...] | list[str]) -> tuple[list[str], list[str]]:
    """'#tag' entries become hashtags, everything else is a search query."""
    hashtags, queries = [], []
    for topic in topics:
        topic = topic.strip()
        if topic.startswith("#"):
            tag = topic.lstrip("#").replace(" ", "")
            if tag:
                hashtags.append(tag)
        elif topic:
            queries.append(topic)
    return hashtags, queries


class ApifySource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ApifyClientAsync(token=settings.apify_token)

    async def _run(self, actor_id: str, run_input: dict[str, Any], timeout_minutes: int = 15) -> list[dict]:
        log.info("Apify %s input=%s", actor_id, run_input)
        run = await self.client.actor(actor_id).call(
            run_input=run_input,
            max_total_charge_usd=Decimal(str(self.settings.apify_max_charge_usd)),
            run_timeout=timedelta(minutes=timeout_minutes),
            logger=None,
        )
        if run is None:
            raise ApifyError(f"Apify не вернул результат запуска {actor_id}")
        page = await self.client.dataset(run.default_dataset_id).list_items(clean=True)
        items = page.items
        if run.status != "SUCCEEDED":
            log.warning("Apify %s finished with %s (%s), %d items", actor_id, run.status, run.status_message, len(items))
            if not items:
                raise ApifyError(f"{actor_id}: {run.status} — {run.status_message or 'нет данных'}")
        log.info("Apify %s -> %d items", actor_id, len(items))
        return items

    # --- TikTok -----------------------------------------------------------

    async def tiktok_topics(self, topics: list[str], per_topic: int) -> list[Video]:
        hashtags, queries = split_topics(topics)
        run_input = {
            "hashtags": hashtags,
            "searchQueries": queries,
            "searchSection": "/video",
            "resultsPerPage": per_topic,
            **NO_DOWNLOADS,
            **self.settings.tiktok_extra_input,
        }
        items = await self._run(self.settings.tiktok_actor, run_input)
        return [v for v in map(tiktok_from_apify, items) if v]

    async def tiktok_profile(self, username: str, limit: int) -> list[Video]:
        run_input = {
            "profiles": [username],
            "resultsPerPage": limit,
            **NO_DOWNLOADS,
            **self.settings.tiktok_extra_input,
        }
        items = await self._run(self.settings.tiktok_actor, run_input)
        return [v for v in map(tiktok_from_apify, items) if v]

    async def tiktok_details(self, urls: list[str]) -> list[Video]:
        """Re-scrape specific videos with the video file and auto-subtitles stored on Apify."""
        run_input = {
            "postURLs": urls,
            **NO_DOWNLOADS,
            "shouldDownloadVideos": True,
            "shouldDownloadSubtitles": True,
            **self.settings.tiktok_extra_input,
        }
        items = await self._run(self.settings.tiktok_actor, run_input)
        return [v for v in map(tiktok_from_apify, items) if v]

    # --- Instagram --------------------------------------------------------

    async def instagram_topics(self, topics: list[str], per_topic: int) -> list[Video]:
        hashtags, queries = split_topics(topics)
        # Instagram has no video search, so search queries become hashtags too.
        hashtags += [q.replace(" ", "").lower() for q in queries]
        run_input = {
            "hashtags": hashtags,
            "resultsType": "reels",
            "resultsLimit": per_topic,
            **self.settings.instagram_hashtag_extra_input,
        }
        items = await self._run(self.settings.instagram_hashtag_actor, run_input)
        return [v for v in map(instagram_from_apify, items) if v]

    async def instagram_profile(self, username: str, limit: int) -> list[Video]:
        run_input = {
            "username": [username],
            "resultsLimit": limit,
            **self.settings.instagram_profile_extra_input,
        }
        items = await self._run(self.settings.instagram_profile_actor, run_input)
        return [v for v in map(instagram_from_apify, items) if v]

    async def instagram_post(self, url: str) -> list[Video]:
        run_input = {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1}
        items = await self._run(self.settings.instagram_post_actor, run_input)
        return [v for v in map(instagram_from_apify, items) if v]

    # --- Platform-neutral helpers -----------------------------------------

    async def profile(self, platform: str, username: str, limit: int) -> list[Video]:
        if platform == TIKTOK:
            return await self.tiktok_profile(username, limit)
        return await self.instagram_profile(username, limit)

    async def single_video(self, platform: str, url: str) -> Video | None:
        videos = await (self.tiktok_details([url]) if platform == TIKTOK else self.instagram_post(url))
        return videos[0] if videos else None

    async def with_media(self, videos: list[Video]) -> list[Video]:
        """Make sure TikTok videos carry a downloadable file and subtitles (Instagram already does)."""
        tiktok = [v for v in videos if v.platform == TIKTOK and not v.video_url]
        if not tiktok:
            return videos
        detailed = {v.id: v for v in await self.tiktok_details([v.url for v in tiktok])}
        result = []
        for video in videos:
            fresh = detailed.get(video.id) if video.platform == TIKTOK else None
            if fresh:
                video.video_url = fresh.video_url or video.video_url
                video.subtitle_url = fresh.subtitle_url or video.subtitle_url
                video.cover_url = fresh.cover_url or video.cover_url
            result.append(video)
        return result


__all__ = ["ApifyError", "ApifySource", "INSTAGRAM", "TIKTOK", "split_topics"]
