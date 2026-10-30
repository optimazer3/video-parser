"""HTTP sessions that go through the optional PROXY (for places where Telegram or Instagram are blocked)."""

from __future__ import annotations

from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector


def http_session(proxy: str | None, timeout: float) -> aiohttp.ClientSession:
    """aiohttp session; with a proxy (socks5://, socks4://, http://) every request goes through it."""
    connector = ProxyConnector.from_url(proxy) if proxy else None
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), connector=connector)


def safe_proxy(proxy: str) -> str:
    """Proxy address for logs, without the login and password."""
    parsed = urlparse(proxy)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.hostname else proxy
