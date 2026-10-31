"""Ranking for the feed and "what went viral" statistics for a profile."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Video

# When a platform hides views (Instagram hashtag results often do),
# estimate them from likes using a typical ~4% like rate.
LIKES_TO_VIEWS = 25
# A video counts as a hit when it got at least this many times the account's median views.
HIT_RATIO = 2.0
# Videos younger than this are still gaining views, so they are not called flops.
MIN_AGE_FOR_FLOP_DAYS = 3


def estimated_views(video: Video) -> int:
    return video.views if video.views is not None else video.likes * LIKES_TO_VIEWS


def feed_score(video: Video, now: datetime | None = None) -> float:
    """Higher is better: fast view growth, beating the author's audience size, strong engagement."""
    now = now or datetime.now(timezone.utc)
    views = estimated_views(video)
    age = max(video.age_days(now) or 7.0, 0.5)
    score = math.log10(views / age + 1)
    if video.author_followers:
        score += 0.5 * math.log10(views / max(video.author_followers, 1000) + 1)
    er = video.engagement_rate
    if er is not None:
        score += 3 * min(er, 0.25)
    if video.saves is not None and video.views:
        score += 10 * min(video.saves / video.views, 0.05)
    return score


def pick_feed(
    videos: list[Video],
    *,
    seen: set[str],
    days: int,
    min_views: int,
    min_likes: int,
    limit: int,
    now: datetime | None = None,
) -> list[Video]:
    now = now or datetime.now(timezone.utc)
    unique: dict[str, Video] = {}
    for video in videos:
        if video.key in seen or video.key in unique:
            continue
        age = video.age_days(now)
        if age is not None and age > days:
            continue
        if video.views is not None and video.views < min_views:
            continue
        if video.views is None and video.likes < min_likes:
            continue
        unique[video.key] = video
    ranked = sorted(unique.values(), key=lambda v: feed_score(v, now), reverse=True)

    # Do not let a single author take over the whole feed.
    picked: list[Video] = []
    per_author: dict[str, int] = {}
    for video in ranked:
        author = f"{video.platform}:{video.author.lower()}"
        if per_author.get(author, 0) >= 2:
            continue
        per_author[author] = per_author.get(author, 0) + 1
        picked.append(video)
        if len(picked) >= limit:
            break
    return picked


FRESH, ALL_TIME = "fresh", "alltime"
# Of the analysed videos, at most this many come from the all-time most popular list; the rest are fresh hits.
ALL_TIME_SLOTS = 2


@dataclass
class ProfileStats:
    videos: list[Video]  # the latest videos: the account's baseline
    median_views: float
    mean_er: float | None
    hits: list[Video]
    flops: list[Video]
    hit_count: int
    posts_per_week: float | None
    ratios: dict[str, float] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)  # hit key -> FRESH / ALL_TIME
    popular: list[Video] = field(default_factory=list)  # all-time most popular videos, if collected

    def ratio(self, video: Video) -> float:
        return self.ratios.get(video.key, 0.0)

    def kind(self, video: Video) -> str:
        return self.kinds.get(video.key, FRESH)

    @property
    def all_videos(self) -> list[Video]:
        """Everything collected, without duplicates: latest videos plus all-time popular ones."""
        seen = {v.key for v in self.videos}
        return self.videos + [v for v in self.popular if v.key not in seen]

    @property
    def period(self) -> tuple[datetime, datetime] | None:
        dates = sorted(v.created_at for v in self.videos if v.created_at)
        return (dates[0], dates[-1]) if dates else None


def profile_stats(
    videos: list[Video], *, top: int, popular: list[Video] | tuple = (), flops: int = 3, now: datetime | None = None
) -> ProfileStats:
    """Hits are measured against the median of the latest videos: fresh outliers first,
    plus up to ALL_TIME_SLOTS of the account's all-time most popular videos when those were collected."""
    now = now or datetime.now(timezone.utc)
    views = [estimated_views(v) for v in videos]
    median = statistics.median(views) if views else 0.0
    everything = videos + [v for v in popular if v.key not in {x.key for x in videos}]
    ratios = {v.key: estimated_views(v) / median if median else 0.0 for v in everything}

    by_ratio = sorted(videos, key=lambda v: ratios[v.key], reverse=True)
    fresh = [v for v in by_ratio if ratios[v.key] >= HIT_RATIO]
    hit_count = len(fresh)
    if len(fresh) < min(3, top):
        # Few clear outliers: still analyse the best performers of this account.
        fresh = by_ratio[: min(3, top)]

    fresh_keys = {v.key for v in fresh}
    all_time = [v for v in sorted(popular, key=estimated_views, reverse=True) if v.key not in fresh_keys]
    slots = min(ALL_TIME_SLOTS, len(all_time), top // 2)
    hits = fresh[: top - slots] + all_time[:slots]
    if len(hits) < top:
        hits += [v for v in fresh[top - slots:] + all_time[slots:]][: top - len(hits)]
    kinds = {v.key: (FRESH if v.key in fresh_keys else ALL_TIME) for v in hits}

    hit_keys = {v.key for v in hits}
    mature = [
        v for v in videos
        if v.key not in hit_keys and (v.age_days(now) or MIN_AGE_FOR_FLOP_DAYS) >= MIN_AGE_FOR_FLOP_DAYS
    ]
    worst = sorted(mature, key=lambda v: ratios[v.key])[:flops]

    ers = [v.engagement_rate for v in videos if v.engagement_rate is not None]
    dates = sorted(v.created_at for v in videos if v.created_at and not v.is_pinned)
    posts_per_week = None
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1.0)
        posts_per_week = (len(dates) - 1) / span_days * 7

    return ProfileStats(
        videos=videos,
        median_views=median,
        mean_er=statistics.mean(ers) if ers else None,
        hits=hits,
        flops=worst,
        hit_count=hit_count,
        posts_per_week=posts_per_week,
        ratios=ratios,
        kinds=kinds,
        popular=list(popular),
    )
