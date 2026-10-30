"""Recognise TikTok / Instagram profile and video links in user messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import INSTAGRAM, TIKTOK
from .net import http_session

URL_RE = re.compile(r"https?://\S+|(?:www\.|vm\.|vt\.|m\.)?(?:tiktok|instagram)\.com/\S+", re.IGNORECASE)
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._]{2,30})$")
TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
INSTAGRAM_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about", "developer", "legal",
}


@dataclass(frozen=True)
class Target:
    platform: str
    kind: str  # "profile" or "video"
    username: str | None = None
    url: str | None = None


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(").,!?»\"'")
    return url if url.lower().startswith("http") else f"https://{url}"


def parse_url(url: str) -> Target | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in parsed.path.split("/") if p]

    if host.endswith("tiktok.com"):
        if not parts or not parts[0].startswith("@"):
            return None
        username = parts[0][1:]
        if len(parts) >= 3 and parts[1] in ("video", "photo") and parts[2].isdigit():
            clean = f"https://www.tiktok.com/@{username}/video/{parts[2]}"
            return Target(TIKTOK, "video", username=username, url=clean)
        return Target(TIKTOK, "profile", username=username)

    if host.endswith("instagram.com"):
        if not parts:
            return None
        if parts[0] in ("p", "reel", "reels", "tv") and len(parts) >= 2:
            return Target(INSTAGRAM, "video", url=f"https://www.instagram.com/{parts[0]}/{parts[1]}/")
        if len(parts) >= 3 and parts[1] in ("reel", "p"):
            # instagram.com/<user>/reel/<code>/
            return Target(INSTAGRAM, "video", username=parts[0], url=f"https://www.instagram.com/reel/{parts[2]}/")
        if parts[0].lower() not in INSTAGRAM_RESERVED:
            return Target(INSTAGRAM, "profile", username=parts[0])
    return None


def parse_handle(text: str) -> str | None:
    """A bare '@username' (platform unknown yet)."""
    match = HANDLE_RE.match((text or "").strip())
    if match and (text.strip().startswith("@") or "." in match.group(1) or "_" in match.group(1)):
        return match.group(1)
    return None


async def resolve(url: str, proxy: str | None = None) -> Target | None:
    """Parse a link, following TikTok short links (vm.tiktok.com/...) to the real address."""
    host = (urlparse(url).hostname or "").lower()
    if host in TIKTOK_SHORT_HOSTS:
        headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
        async with http_session(proxy, timeout=15) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                url = str(response.url)
    return parse_url(url)
