"""SQLite storage: already-sent videos, cached video data for buttons, per-user settings."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from .models import Video

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS videos (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    saved_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    saved_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def seen_keys(self, user_id: int) -> set[str]:
        async with self.db.execute("SELECT key FROM seen WHERE user_id = ?", (user_id,)) as cursor:
            return {row[0] for row in await cursor.fetchall()}

    async def mark_seen(self, user_id: int, keys: list[str]) -> None:
        now = int(time.time())
        await self.db.executemany(
            "INSERT OR REPLACE INTO seen (user_id, key, sent_at) VALUES (?, ?, ?)",
            [(user_id, key, now) for key in keys],
        )
        await self.db.commit()

    async def save_videos(self, videos: list[Video]) -> None:
        now = int(time.time())
        await self.db.executemany(
            "INSERT OR REPLACE INTO videos (key, data, saved_at) VALUES (?, ?, ?)",
            [(v.key, json.dumps(v.to_dict(), ensure_ascii=False), now) for v in videos],
        )
        # Keep the cache small: buttons older than 30 days stop working, which is fine.
        await self.db.execute("DELETE FROM videos WHERE saved_at < ?", (now - 30 * 86400,))
        await self.db.commit()

    async def get_video(self, key: str) -> Video | None:
        async with self.db.execute("SELECT data FROM videos WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return Video.from_dict(json.loads(row[0])) if row else None

    async def get_profile(
        self, platform: str, username: str, max_age_hours: float
    ) -> tuple[list[Video], list[Video]] | None:
        """(latest, all-time popular) videos of a profile scraped less than max_age_hours ago (saves Apify runs)."""
        if max_age_hours <= 0:
            return None
        async with self.db.execute(
            "SELECT data FROM profiles WHERE key = ? AND saved_at >= ?",
            (f"{platform}:{username.lower()}", int(time.time() - max_age_hours * 3600)),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if isinstance(data, list):  # cache written before the popular list existed
            data = {"latest": data, "popular": []}
        return ([Video.from_dict(v) for v in data["latest"]], [Video.from_dict(v) for v in data["popular"]])

    async def save_profile(self, platform: str, username: str, latest: list[Video], popular: list[Video]) -> None:
        data = {"latest": [v.to_dict() for v in latest], "popular": [v.to_dict() for v in popular]}
        await self.db.execute(
            "INSERT OR REPLACE INTO profiles (key, data, saved_at) VALUES (?, ?, ?)",
            (f"{platform}:{username.lower()}", json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        await self.db.commit()

    async def get_setting(self, user_id: int, name: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM settings WHERE user_id = ? AND name = ?", (user_id, name)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_setting(self, user_id: int, name: str, value: str | None) -> None:
        if value is None:
            await self.db.execute("DELETE FROM settings WHERE user_id = ? AND name = ?", (user_id, name))
        else:
            await self.db.execute(
                "INSERT OR REPLACE INTO settings (user_id, name, value) VALUES (?, ?, ?)", (user_id, name, value)
            )
        await self.db.commit()

    async def get_topics(self, user_id: int, default: tuple[str, ...]) -> list[str]:
        value = await self.get_setting(user_id, "topics")
        return json.loads(value) if value else list(default)

    async def set_topics(self, user_id: int, topics: list[str] | None) -> None:
        await self.set_setting(user_id, "topics", json.dumps(topics, ensure_ascii=False) if topics else None)
