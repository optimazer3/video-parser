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

from .llm import LLMClient
from .models import Video

log = logging.getLogger(__name__)

MAX_VIDEO_BYTES = 150 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# The hook lives in the first seconds, so frames are denser there.
HOOK_TIMESTAMPS = (0.0, 1.0, 2.5)
# Speech recognition gets at most this much audio (Polza accepts request bodies up to ~15 MB).
MAX_AUDIO_SECONDS = 600
# Whole-video mode sends a small copy: the model samples about one frame per second anyway,
# so 480p at 12 fps loses nothing and keeps five clips well under the request size limit.
MAX_CLIP_SECONDS = 90


@dataclass
class VideoMedia:
    frames: list[tuple[float, bytes]] = field(default_factory=list)  # (second, jpeg)
    transcript: str | None = None
    transcript_source: str | None = None
    duration: float | None = None
    error: str | None = None
    cover_only: bool = False  # the video itself could not be downloaded, frames hold just the cover
    clip: bytes | None = None  # small MP4 with sound for models that watch the whole video


class MediaProcessor:
    def __init__(
        self,
        apify_token: str,
        frames_per_video: int = 6,
        llm: LLMClient | None = None,
        transcribe_model: str | None = None,
        proxy: str | None = None,
    ):
        self.apify_token = apify_token
        self.proxy = proxy
        self.frames_per_video = frames_per_video
        self.llm = llm
        self.transcribe_model = transcribe_model
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        if not self.has_ffmpeg:
            log.warning("ffmpeg не найден — кадры из видео извлекаться не будут")

    async def process(self, video: Video, session: aiohttp.ClientSession, make_clip: bool = False) -> VideoMedia:
        media = VideoMedia(duration=video.duration)
        if video.transcript:
            media.transcript, media.transcript_source = video.transcript, "платформа"
        elif video.subtitle_url:
            try:
                media.transcript = await self._subtitles(video.subtitle_url, session)
                media.transcript_source = "автосубтитры TikTok"
            except Exception as exc:  # noqa: BLE001 - subtitles are optional
                log.warning("subtitles failed for %s: %s", video.key, exc)

        with tempfile.TemporaryDirectory(prefix="vp_") as tmp:
            try:
                path = await self._fetch_video(video, Path(tmp), session)
            except Exception as exc:  # noqa: BLE001
                log.warning("download failed for %s: %s", video.key, exc)
                media.error = f"не удалось скачать видео ({short_error(exc)})"
                await self._cover_frame(video, media, Path(tmp), session)
                return media

            if self.has_ffmpeg:
                if not media.duration:
                    media.duration = await probe_duration(path)
                try:
                    media.frames = await extract_frames(path, media.duration, self.frames_per_video, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    log.warning("frames failed for %s: %s", video.key, exc)
                if make_clip:
                    media.clip = await compress_clip(path, Path(tmp) / "clip.mp4")

            if not media.transcript and self.has_ffmpeg and self.llm and self.transcribe_model:
                try:
                    media.transcript = await self._transcribe(path, Path(tmp))
                    media.transcript_source = "распознавание речи"
                except Exception as exc:  # noqa: BLE001
                    log.warning("transcription failed for %s: %s", video.key, exc)
        return media

    async def _fetch_video(self, video: Video, workdir: Path, session: aiohttp.ClientSession) -> Path:
        if video.video_url:
            path = workdir / "video.mp4"
            await self._download(video.video_url, path, session)
            return path
        # No direct file link (TikTok): yt-dlp downloads it for free.
        return await asyncio.to_thread(ytdlp_download, video.url, workdir, self.proxy)

    async def _cover_frame(self, video: Video, media: VideoMedia, workdir: Path, session: aiohttp.ClientSession) -> None:
        """When the video is unavailable, give the model at least the cover."""
        if not video.cover_url or not self.has_ffmpeg:
            return
        try:
            raw = workdir / "cover"
            await self._download(video.cover_url, raw, session)
            jpeg = await to_jpeg(raw, workdir / "cover.jpg")
        except Exception as exc:  # noqa: BLE001
            log.warning("cover failed for %s: %s", video.key, exc)
            return
        if jpeg:
            media.frames, media.cover_only = [(0.0, jpeg)], True

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

    async def _transcribe(self, video_path: Path, workdir: Path) -> str | None:
        audio = await extract_audio(video_path, workdir)
        if audio is None:
            return None  # no sound track
        return await self.llm.transcribe(audio, self.transcribe_model)


class _YtDlpLogger:
    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def _ytdlp_options(proxy: str | None) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "logger": _YtDlpLogger(),
    }
    if proxy:
        options["proxy"] = proxy
    return options


def ytdlp_info(url: str, proxy: str | None = None) -> dict:
    """Video metadata (views, likes, caption, ...) without downloading and without Apify."""
    import yt_dlp

    with yt_dlp.YoutubeDL({**_ytdlp_options(proxy), "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def ytdlp_download(url: str, workdir: Path, proxy: str | None = None) -> Path:
    import yt_dlp

    options = {
        **_ytdlp_options(proxy),
        "outtmpl": str(workdir / "video.%(ext)s"),
        "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "max_filesize": MAX_VIDEO_BYTES,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)
    files = sorted(workdir.glob("video.*"))
    if not files:
        raise RuntimeError("yt-dlp не сохранил файл")
    return files[0]


def short_error(exc: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).removeprefix("ERROR: ").strip()
    return text[:200] or type(exc).__name__


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


async def compress_clip(source: Path, out: Path) -> bytes | None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-t", str(MAX_CLIP_SECONDS),
        "-vf", "scale=-2:'min(480,ih)',fps=12", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
        "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0 or not out.exists():
        log.warning("ffmpeg clip failed: %s", err.decode(errors="replace")[:200])
        return None
    return out.read_bytes()


async def to_jpeg(source: Path, out: Path) -> bytes | None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-frames:v", "1", "-vf", "scale=512:-2",
        "-q:v", "4", str(out), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    return out.read_bytes() if process.returncode == 0 and out.exists() else None


async def extract_audio(path: Path, workdir: Path) -> bytes | None:
    """Mono 16 kHz MP3 — small enough to send for speech recognition."""
    out = workdir / "audio.mp3"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "48k", "-t", str(MAX_AUDIO_SECONDS), str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.debug("ffmpeg audio failed: %s", err.decode(errors="replace")[:200])
        return None
    return out.read_bytes()


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
