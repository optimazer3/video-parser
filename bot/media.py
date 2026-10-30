"""Downloading videos, cutting key frames with ffmpeg and getting the speech as text."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .models import Video

log = logging.getLogger(__name__)

MAX_VIDEO_BYTES = 150 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# The hook lives in the first seconds, so frames are denser there.
HOOK_TIMESTAMPS = (0.0, 1.0, 2.5)


@dataclass
class VideoMedia:
    frames: list[tuple[float, bytes]] = field(default_factory=list)  # (second, jpeg)
    transcript: str | None = None
    transcript_source: str | None = None
    duration: float | None = None
    error: str | None = None


class MediaProcessor:
    def __init__(self, apify_token: str, frames_per_video: int = 6, whisper_model: str | None = None):
        self.apify_token = apify_token
        self.frames_per_video = frames_per_video
        self.whisper_model = whisper_model
        self._whisper = None
        self._whisper_lock = asyncio.Lock()
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        if not self.has_ffmpeg:
            log.warning("ffmpeg не найден — кадры из видео извлекаться не будут")

    async def process(self, video: Video, session: aiohttp.ClientSession) -> VideoMedia:
        media = VideoMedia(duration=video.duration)
        if video.transcript:
            media.transcript, media.transcript_source = video.transcript, "платформа"
        elif video.subtitle_url:
            try:
                media.transcript = await self._subtitles(video.subtitle_url, session)
                media.transcript_source = "автосубтитры TikTok"
            except Exception as exc:  # noqa: BLE001 - subtitles are optional
                log.warning("subtitles failed for %s: %s", video.key, exc)

        if not video.video_url:
            media.error = "нет ссылки на файл видео"
            return media

        with tempfile.TemporaryDirectory(prefix="vp_") as tmp:
            path = Path(tmp) / "video.mp4"
            try:
                await self._download(video.video_url, path, session)
            except Exception as exc:  # noqa: BLE001
                log.warning("download failed for %s: %s", video.key, exc)
                media.error = f"не удалось скачать видео ({exc})"
                return media

            if self.has_ffmpeg:
                if not media.duration:
                    media.duration = await probe_duration(path)
                try:
                    media.frames = await extract_frames(path, media.duration, self.frames_per_video, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    log.warning("frames failed for %s: %s", video.key, exc)

            if not media.transcript and self.whisper_model:
                try:
                    media.transcript = await self._transcribe(path)
                    media.transcript_source = "распознавание речи (Whisper)"
                except Exception as exc:  # noqa: BLE001
                    log.warning("whisper failed for %s: %s", video.key, exc)
        return media

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        host = urlparse(url).hostname or ""
        if host.endswith("apify.com"):
            headers["Authorization"] = f"Bearer {self.apify_token}"
        elif "tiktok" in host:
            headers["Referer"] = "https://www.tiktok.com/"
        return headers

    async def _download(self, url: str, path: Path, session: aiohttp.ClientSession) -> None:
        async with session.get(url, headers=self._headers(url)) as response:
            response.raise_for_status()
            size = 0
            with path.open("wb") as file:
                async for chunk in response.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES:
                        raise ValueError("файл слишком большой")
                    file.write(chunk)

    async def _subtitles(self, url: str, session: aiohttp.ClientSession) -> str | None:
        async with session.get(url, headers=self._headers(url)) as response:
            response.raise_for_status()
            raw = await response.text(errors="replace")
        return vtt_to_text(raw) or None

    async def _transcribe(self, path: Path) -> str | None:
        async with self._whisper_lock:
            return await asyncio.to_thread(self._transcribe_sync, str(path))

    def _transcribe_sync(self, path: str) -> str | None:
        if self._whisper is None:
            from faster_whisper import WhisperModel  # optional dependency

            self._whisper = WhisperModel(self.whisper_model, device="cpu", compute_type="int8")
        segments, _info = self._whisper.transcribe(path, vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text or None


def vtt_to_text(raw: str) -> str:
    """WebVTT/SRT -> plain text without timings and repeated lines."""
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def frame_timestamps(duration: float | None, count: int) -> list[float]:
    if not duration or duration <= 0:
        return list(HOOK_TIMESTAMPS[:count])
    stamps = [t for t in HOOK_TIMESTAMPS if t < duration - 0.2]
    rest = max(count - len(stamps), 0)
    for i in range(1, rest + 1):
        # Spread the remaining frames over the body of the video, ending near the final CTA.
        stamps.append(round(3.0 + (duration - 3.5) * i / rest, 1) if duration > 4 else duration * i / (rest + 1))
    unique = sorted({round(t, 1) for t in stamps if 0 <= t < duration})
    return unique[:count]


async def probe_duration(path: Path) -> float | None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def extract_frames(path: Path, duration: float | None, count: int, workdir: Path) -> list[tuple[float, bytes]]:
    frames = []
    for i, second in enumerate(frame_timestamps(duration, count)):
        out = workdir / f"frame_{i}.jpg"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{second:.2f}", "-i", str(path),
            "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "4", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await process.communicate()
        if process.returncode == 0 and out.exists() and out.stat().st_size > 0:
            frames.append((second, out.read_bytes()))
        else:
            log.debug("ffmpeg frame %.1fs failed: %s", second, err.decode(errors="replace")[:200])
    return frames
