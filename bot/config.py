"""Bot settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_TOPICS = (
    "#нейросети",
    "#нейросеть",
    "#искусственныйинтеллект",
    "#chatgpt",
    "#aitools",
    "нейросети для работы",
)


def _str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = _str(name)
    return float(value) if value else default


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = _str(name)
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _json(name: str) -> dict:
    value = _str(name)
    if not value:
        return {}
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return data


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    apify_token: str
    llm_api_key: str | None
    allowed_user_ids: frozenset[int]

    llm_base_url: str
    llm_model: str
    llm_reasoning_effort: str | None
    transcribe_model: str | None
    proxy: str | None

    tiktok_actor: str
    instagram_profile_actor: str
    instagram_hashtag_actor: str
    instagram_post_actor: str
    tiktok_extra_input: dict
    instagram_profile_extra_input: dict
    instagram_hashtag_extra_input: dict
    apify_max_charge_usd: float

    default_topics: tuple[str, ...]
    feed_platforms: tuple[str, ...]
    feed_days: int
    feed_size: int
    feed_min_views: int
    feed_min_likes: int
    feed_results_per_topic: int

    profile_videos: int
    analyze_top: int
    frames_per_video: int

    digest_time: str | None
    timezone: str
    db_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        telegram_token = _str("TELEGRAM_BOT_TOKEN")
        apify_token = _str("APIFY_TOKEN")
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", telegram_token), ("APIFY_TOKEN", apify_token)) if not v]
        if missing:
            raise SystemExit(f"Не заданы переменные окружения: {', '.join(missing)} (см. .env.example)")
        proxy = _str("PROXY") or _str("MEDIA_PROXY")
        if proxy and "://" not in proxy:
            raise SystemExit(f"PROXY в .env должен начинаться со схемы, например socks5://{proxy} или http://{proxy}")

        return cls(
            telegram_token=telegram_token,
            apify_token=apify_token,
            llm_api_key=_str("POLZA_API_KEY"),
            allowed_user_ids=frozenset(int(x) for x in _list("ALLOWED_USER_IDS")),
            llm_base_url=_str("LLM_BASE_URL", "https://polza.ai/api/v1"),
            llm_model=_str("LLM_MODEL", "anthropic/claude-sonnet-5.5"),
            llm_reasoning_effort=_str("LLM_REASONING_EFFORT", "high"),
            transcribe_model=_str("TRANSCRIBE_MODEL", "openai/gpt-4o-mini-transcribe"),
            proxy=proxy,
            tiktok_actor=_str("APIFY_TIKTOK_ACTOR", "clockworks/tiktok-scraper"),
            instagram_profile_actor=_str("APIFY_INSTAGRAM_PROFILE_ACTOR", "apify/instagram-reel-scraper"),
            instagram_hashtag_actor=_str("APIFY_INSTAGRAM_HASHTAG_ACTOR", "apify/instagram-hashtag-scraper"),
            instagram_post_actor=_str("APIFY_INSTAGRAM_POST_ACTOR", "apify/instagram-scraper"),
            tiktok_extra_input=_json("APIFY_TIKTOK_EXTRA_INPUT"),
            instagram_profile_extra_input=_json("APIFY_INSTAGRAM_PROFILE_EXTRA_INPUT"),
            instagram_hashtag_extra_input=_json("APIFY_INSTAGRAM_HASHTAG_EXTRA_INPUT"),
            apify_max_charge_usd=_float("APIFY_MAX_CHARGE_USD", 2.0),
            default_topics=_list("TOPICS", DEFAULT_TOPICS),
            feed_platforms=_list("FEED_PLATFORMS", ("tiktok", "instagram")),
            feed_days=_int("FEED_DAYS", 7),
            feed_size=_int("FEED_SIZE", 10),
            feed_min_views=_int("FEED_MIN_VIEWS", 20_000),
            feed_min_likes=_int("FEED_MIN_LIKES", 500),
            feed_results_per_topic=_int("FEED_RESULTS_PER_TOPIC", 30),
            profile_videos=_int("PROFILE_VIDEOS", 40),
            analyze_top=_int("ANALYZE_TOP", 5),
            frames_per_video=_int("FRAMES_PER_VIDEO", 6),
            digest_time=_str("DIGEST_TIME"),
            timezone=_str("TIMEZONE", "Europe/Moscow"),
            db_path=_str("DB_PATH", "data/bot.sqlite3"),
        )
