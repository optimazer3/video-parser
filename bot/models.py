"""A platform-neutral video record and converters from Apify actor output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

TIKTOK = "tiktok"
INSTAGRAM = "instagram"
PLATFORM_LABELS = {TIKTOK: "TikTok", INSTAGRAM: "Reels"}


@dataclass
class Video:
    platform: str
    id: str
    url: str
    author: str
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    duration: float | None = None
    views: int | None = None
    likes: int = 0
    comments: int = 0
    shares: int | None = None
    saves: int | None = None
    author_followers: int | None = None
    music: str | None = None
    cover_url: str | None = None
    video_url: str | None = None
    subtitle_url: str | None = None
    transcript: str | None = None
    is_pinned: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @property
    def interactions(self) -> int:
        return self.likes + self.comments + (self.shares or 0) + (self.saves or 0)

    @property
    def engagement_rate(self) -> float | None:
        if not self.views:
            return None
        return self.interactions / self.views

    def age_days(self, now: datetime | None = None) -> float | None:
        if not self.created_at:
            return None
        now = now or datetime.now(timezone.utc)
        return max((now - self.created_at).total_seconds() / 86400, 0.0)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Video":
        data = dict(data)
        if data.get("created_at"):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**data)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    # Instagram reports -1 when the author hides likes.
    return number if number >= 0 else None


def _date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tags(raw: Any) -> list[str]:
    tags = []
    for tag in raw or []:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if name:
            tags.append(str(name).lstrip("#"))
    return tags


def tiktok_from_apify(item: dict[str, Any]) -> Video | None:
    """Convert an item from clockworks/tiktok-scraper. Returns None for errors and slideshows."""
    video_id = item.get("id")
    if not video_id or item.get("error") or item.get("isSlideshow"):
        return None
    author = item.get("authorMeta") or {}
    meta = item.get("videoMeta") or {}
    music = item.get("musicMeta") or {}
    username = author.get("name") or author.get("uniqueId") or ""

    subtitle_url = None
    for sub in meta.get("subtitleLinks") or []:
        link = sub.get("downloadLink") or sub.get("tiktokLink")
        if not link:
            continue
        language = str(sub.get("language") or "").lower()
        # Prefer Russian, then English, then whatever comes first.
        if language.startswith("rus"):
            subtitle_url = link
            break
        if subtitle_url is None or language.startswith("eng"):
            subtitle_url = link

    media_urls = item.get("mediaUrls") or []
    music_name = " — ".join(x for x in (music.get("musicAuthor"), music.get("musicName")) if x)
    if music_name and music.get("musicOriginal"):
        music_name += " (оригинальный звук)"

    return Video(
        platform=TIKTOK,
        id=str(video_id),
        url=item.get("webVideoUrl") or f"https://www.tiktok.com/@{username}/video/{video_id}",
        author=username,
        caption=item.get("text") or "",
        hashtags=_tags(item.get("hashtags")),
        created_at=_date(item.get("createTimeISO") or item.get("createTime")),
        duration=meta.get("duration"),
        views=_int(item.get("playCount")),
        likes=_int(item.get("diggCount")) or 0,
        comments=_int(item.get("commentCount")) or 0,
        shares=_int(item.get("shareCount")),
        saves=_int(item.get("collectCount")),
        author_followers=_int(author.get("fans")),
        music=music_name or None,
        cover_url=meta.get("coverUrl") or meta.get("originalCoverUrl"),
        video_url=media_urls[0] if media_urls else None,
        subtitle_url=subtitle_url,
        is_pinned=bool(item.get("isPinned")),
    )


def instagram_from_apify(item: dict[str, Any]) -> Video | None:
    """Convert an item from the Apify Instagram scrapers. Returns None for anything that is not a video."""
    short_code = item.get("shortCode")
    video_id = item.get("id") or short_code
    is_video = item.get("type") == "Video" or item.get("productType") == "clips" or item.get("videoUrl")
    if not video_id or item.get("error") or not is_video:
        return None

    music = item.get("musicInfo") or {}
    music_name = " — ".join(x for x in (music.get("artist_name"), music.get("song_name")) if x)
    if music.get("uses_original_audio"):
        music_name = (music_name + " " if music_name else "") + "(оригинальный звук)"

    url = item.get("url") or (f"https://www.instagram.com/reel/{short_code}/" if short_code else "")
    return Video(
        platform=INSTAGRAM,
        id=str(video_id),
        url=url,
        author=item.get("ownerUsername") or "",
        caption=item.get("caption") or "",
        hashtags=_tags(item.get("hashtags")),
        created_at=_date(item.get("timestamp")),
        duration=item.get("videoDuration"),
        views=_int(item.get("videoPlayCount")) or _int(item.get("videoViewCount")),
        likes=_int(item.get("likesCount")) or 0,
        comments=_int(item.get("commentsCount")) or 0,
        shares=_int(item.get("sharesCount")),
        saves=None,
        author_followers=_int(item.get("ownerFollowersCount")),
        music=music_name or None,
        cover_url=item.get("displayUrl"),
        video_url=item.get("videoUrl"),
        transcript=item.get("transcript") or None,
        is_pinned=bool(item.get("isPinned")),
    )
