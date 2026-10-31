"""Bot settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

# Every topic is paid on Apify (FEED_RESULTS_PER_TOPIC videos per platform), so keep the list short.
DEFAULT_TOPICS = (
    "#нейросети",
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


def duplicate_keys(path: str) -> list[str]:
    """Keys written twice in .env: only the last value counts, which is easy to miss."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", errors="replace") as file:
        keys = [m.group(1) for line in file if (m := re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line))]
    return [key for key, count in Counter(keys).items() if count > 1]


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    apify_token: str
    llm_api_key: str | None
    allowed_user_ids: frozenset[int]

    llm_base_url: str
    llm_model: str
    llm_reasoning_effort: str | None
    llm_video: bool
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
    profile_popular: int
    report_format: str
    profile_cache_hours: float
    analyze_top: int
    frames_per_video: int

    digest_time: str | None
    timezone: str
    db_path: str
    env_path: str
    env_duplicates: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        # The .env file wins over variables already set in the system, so what you see there is what runs.
        env_path = find_dotenv(usecwd=True) or find_dotenv()
        load_dotenv(env_path or None, override=True)
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
            llm_video=_str("LLM_VIDEO_MODE", "auto").lower() not in ("off", "0", "false", "no"),
            transcribe_model=_str("TRANSCRIBE_MODEL", "openai/gpt-4o-mini-transcribe"),
            proxy=proxy,
            tiktok_actor=_str("APIFY_TIKTOK_ACTOR", "clockworks/tiktok-scraper"),
            instagram_profile_actor=_str("APIFY_INSTAGRAM_PROFILE_ACTOR", "apify/instagram-reel-scraper"),
            instagram_hashtag_actor=_str("APIFY_INSTAGRAM_HASHTAG_ACTOR", "apify/instagram-hashtag-scraper"),
            instagram_post_actor=_str("APIFY_INSTAGRAM_POST_ACTOR", "apify/instagram-scraper"),
            tiktok_extra_input=_json("APIFY_TIKTOK_EXTRA_INPUT"),
            instagram_profile_extra_input=_json("APIFY_INSTAGRAM_PROFILE_EXTRA_INPUT"),
            instagram_hashtag_extra_input=_json("APIFY_INSTAGRAM_HASHTAG_EXTRA_INPUT"),
            apify_max_charge_usd=_float("APIFY_MAX_CHARGE_USD", 0.5),
            default_topics=_list("TOPICS", DEFAULT_TOPICS),
            feed_platforms=_list("FEED_PLATFORMS", ("tiktok", "instagram")),
            feed_days=_int("FEED_DAYS", 7),
            feed_size=_int("FEED_SIZE", 10),
            feed_min_views=_int("FEED_MIN_VIEWS", 20_000),
            feed_min_likes=_int("FEED_MIN_LIKES", 500),
            feed_results_per_topic=_int("FEED_RESULTS_PER_TOPIC", 10),
            profile_videos=_int("PROFILE_VIDEOS", 30),
            profile_popular=_int("PROFILE_POPULAR", 5),
            report_format=(_str("REPORT_FORMAT", "pdf") or "pdf").lower(),
            profile_cache_hours=_float("PROFILE_CACHE_HOURS", 24),
            analyze_top=_int("ANALYZE_TOP", 5),
            frames_per_video=_int("FRAMES_PER_VIDEO", 6),
            digest_time=_str("DIGEST_TIME"),
            timezone=_str("TIMEZONE", "Europe/Moscow"),
            db_path=_str("DB_PATH", "data/bot.sqlite3"),
            env_path=env_path,
            env_duplicates=tuple(duplicate_keys(env_path)),
        )
