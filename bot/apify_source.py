"""Fetching videos through Apify actors."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClientAsync

from . import costs
from .config import Settings
from .models import INSTAGRAM, TIKTOK, Video, instagram_from_apify, tiktok_from_apify

log = logging.getLogger(__name__)

# Video files are downloaded for free with yt-dlp (see media.py), so Apify only collects metadata.
NO_DOWNLOADS = {
    "shouldDownloadVideos": False,
    "shouldDownloadCovers": False,
    "shouldDownloadSubtitles": False,
    "shouldDownloadSlideshowImages": False,
}
PER_RESULT_MODELS = ("PAY_PER_EVENT", "PRICE_PER_DATASET_ITEM")


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


def run_cost_usd(run: Any, item_count: int) -> float:
    """Estimate what the user is charged for a run: per-result/per-event price plus platform usage if billed."""
    info = run.pricing_info
    model = getattr(info, "pricing_model", None)
    cost = 0.0
    if model == "PAY_PER_EVENT":
        events = (info.pricing_per_event.actor_charge_events or {}) if info.pricing_per_event else {}
        for name, count in (run.charged_event_counts or {}).items():
            event = events.get(name)
            if event and event.event_price_usd:
                cost += event.event_price_usd * count
    elif model == "PRICE_PER_DATASET_ITEM":
        cost += (info.price_per_unit_usd or 0.0) * item_count
    billing = run.platform_usage_billing_model
    if billing == "USER" or (billing is None and model not in PER_RESULT_MODELS):
        cost += run.usage_total_usd or 0.0
    return cost


class ApifySource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ApifyClientAsync(token=settings.apify_token)

    async def _run(self, actor_id: str, run_input: dict[str, Any], max_items: int, timeout_minutes: int = 15) -> list[dict]:
        log.info("Apify %s input=%s", actor_id, run_input)
        run = await self.client.actor(actor_id).call(
            run_input=run_input,
            # Hard caps: pay-per-result actors stop at max_items, pay-per-event ones at the dollar limit.
            max_items=max_items,
            max_total_charge_usd=Decimal(str(self.settings.apify_max_charge_usd)),
            run_timeout=timedelta(minutes=timeout_minutes),
            logger=None,
        )
        if run is None:
            raise ApifyError(f"Apify не вернул результат запуска {actor_id}")
        page = await self.client.dataset(run.default_dataset_id).list_items(clean=True)
        items = page.items

        try:
            # Usage and charged events are final only after the run ends, so read the run again.
            finished = await self.client.run(run.id).get() or run
            usd = run_cost_usd(finished, page.total or len(items))
        except Exception:  # noqa: BLE001 - the cost is informational
            log.exception("could not read the cost of run %s", run.id)
            usd = 0.0
        costs.add_apify(actor_id, usd)
        log.info("Apify %s -> %d items, ≈$%.3f", actor_id, len(items), usd)

        if run.status != "SUCCEEDED":
            log.warning("Apify %s finished with %s (%s)", actor_id, run.status, run.status_message)
            if not items:
                raise ApifyError(f"{actor_id}: {run.status} — {run.status_message or 'нет данных'}")
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
        items = await self._run(self.settings.tiktok_actor, run_input, per_topic * max(len(topics), 1))
        return [v for v in map(tiktok_from_apify, items) if v]

    async def tiktok_profile(self, username: str, limit: int) -> list[Video]:
        run_input = {
            "profiles": [username],
            "resultsPerPage": limit,
            **NO_DOWNLOADS,
            **self.settings.tiktok_extra_input,
        }
        items = await self._run(self.settings.tiktok_actor, run_input, limit)
        return [v for v in map(tiktok_from_apify, items) if v]

    async def tiktok_post(self, url: str) -> list[Video]:
        run_input = {"postURLs": [url], **NO_DOWNLOADS, **self.settings.tiktok_extra_input}
        items = await self._run(self.settings.tiktok_actor, run_input, 1)
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
        items = await self._run(self.settings.instagram_hashtag_actor, run_input, per_topic * max(len(hashtags), 1))
        return [v for v in map(instagram_from_apify, items) if v]

    async def instagram_profile(self, username: str, limit: int) -> list[Video]:
        run_input = {
            "username": [username],
            "resultsLimit": limit,
            **self.settings.instagram_profile_extra_input,
        }
        items = await self._run(self.settings.instagram_profile_actor, run_input, limit)
        return [v for v in map(instagram_from_apify, items) if v]

    async def instagram_post(self, url: str) -> list[Video]:
        run_input = {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1}
        items = await self._run(self.settings.instagram_post_actor, run_input, 1)
        return [v for v in map(instagram_from_apify, items) if v]

    # --- Platform-neutral helpers -----------------------------------------

    async def profile(self, platform: str, username: str, limit: int) -> list[Video]:
        if platform == TIKTOK:
            return await self.tiktok_profile(username, limit)
        return await self.instagram_profile(username, limit)

    async def single_video(self, platform: str, url: str) -> Video | None:
        videos = await (self.tiktok_post(url) if platform == TIKTOK else self.instagram_post(url))
        return videos[0] if videos else None


__all__ = ["ApifyError", "ApifySource", "INSTAGRAM", "TIKTOK", "run_cost_usd", "split_topics"]
