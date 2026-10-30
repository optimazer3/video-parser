"""Telegram message texts (HTML parse mode)."""

from __future__ import annotations

from html import escape

from .analyzer import ProfileReport, SingleReport, VideoAnalysis
from .metrics import ProfileStats, estimated_views
from .models import PLATFORM_LABELS, TIKTOK, Video

TELEGRAM_LIMIT = 4096
PLATFORM_ICONS = {TIKTOK: "🎵", "instagram": "📸"}


def num(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            value = f"{n / size:.1f}".rstrip("0").rstrip(".")
            return f"{value}{suffix}"
    return str(int(n))


def duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def age(video: Video) -> str:
    days = video.age_days()
    if days is None:
        return ""
    if days < 1:
        return f"{max(int(days * 24), 1)} ч назад"
    return f"{int(days)} дн назад"


def short(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stats_line(video: Video) -> str:
    parts = [f"👁 {num(video.views) if video.views is not None else '~' + num(estimated_views(video))}"]
    parts.append(f"❤️ {num(video.likes)}")
    parts.append(f"💬 {num(video.comments)}")
    if video.shares is not None:
        parts.append(f"🔁 {num(video.shares)}")
    if video.saves is not None:
        parts.append(f"💾 {num(video.saves)}")
    return " · ".join(parts)


def feed_card(video: Video, index: int) -> str:
    icon = PLATFORM_ICONS.get(video.platform, "🎬")
    label = PLATFORM_LABELS.get(video.platform, video.platform)
    lines = [f"<b>{index}. {icon} {label} · @{escape(video.author)}</b>", stats_line(video)]
    details = []
    if video.engagement_rate is not None:
        details.append(f"ER {video.engagement_rate * 100:.1f}%")
    if video.author_followers and video.views:
        details.append(f"×{video.views / max(video.author_followers, 1):.1f} к подписчикам")
    details += [x for x in (age(video), duration(video.duration)) if x]
    if details:
        lines.append("📈 " + " · ".join(details))
    if video.caption.strip():
        lines.append(f"<i>{escape(short(video.caption))}</i>")
    lines.append(escape(video.url))
    return "\n".join(lines)


def profile_overview(platform: str, username: str, stats: ProfileStats) -> str:
    label = PLATFORM_LABELS.get(platform, platform)
    lines = [
        f"<b>📊 @{escape(username)} · {label}</b>",
        f"Роликов разобрано: {len(stats.videos)}",
        f"Медиана просмотров: {num(stats.median_views)}",
    ]
    if stats.mean_er is not None:
        lines.append(f"Средний ER: {stats.mean_er * 100:.1f}%")
    if stats.posts_per_week:
        lines.append(f"Публикаций в неделю: ~{stats.posts_per_week:.1f}")
    lines.append(f"Залетевших (×2 к медиане и выше): {stats.hit_count}")
    lines.append("")
    lines.append("<b>🔥 Лучшие ролики</b>")
    for i, video in enumerate(stats.hits, 1):
        lines.append(
            f"{i}. <a href=\"{escape(video.url, quote=True)}\">{escape(short(video.caption, 60) or 'без подписи')}</a>"
            f" — {num(estimated_views(video))} просм., ×{stats.ratio(video):.1f} к медиане"
        )
    return "\n".join(lines)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {escape(item)}" for item in items if item.strip())


def video_analysis(analysis: VideoAnalysis, video: Video | None, ratio: float | None = None) -> str:
    header = f"<b>🎬 Ролик №{analysis.video_number}</b>"
    if video:
        header += f" · {num(estimated_views(video))} просм."
        if ratio:
            header += f" · ×{ratio:.1f}"
        header += f"\n{escape(video.url)}"
    parts = [
        header,
        f"<b>Почему залетел</b>\n{escape(analysis.why_it_worked)}",
        f"<b>Хук</b>\n{escape(analysis.hook)}",
        f"<b>Структура и подача</b>\n{escape(analysis.structure)}",
    ]
    if analysis.what_to_improve:
        parts.append(f"<b>Что улучшить</b>\n{_bullets(analysis.what_to_improve)}")
    if analysis.repeat_this:
        parts.append(f"<b>Что повторять</b>\n{_bullets(analysis.repeat_this)}")
    return "\n\n".join(parts)


def profile_report(report: ProfileReport, stats: ProfileStats) -> list[str]:
    messages = [f"<b>🧠 Итог по аккаунту</b>\n{escape(report.summary)}"]
    if report.hit_patterns:
        messages[0] += f"\n\n<b>Что общего у хитов</b>\n{_bullets(report.hit_patterns)}"
    for analysis in report.videos:
        index = analysis.video_number - 1
        video = stats.hits[index] if 0 <= index < len(stats.hits) else None
        messages.append(video_analysis(analysis, video, stats.ratio(video) if video else None))
    tail = []
    if report.weak_spots:
        tail.append(f"<b>📉 Что тянет вниз</b>\n{_bullets(report.weak_spots)}")
    if report.recommendations:
        tail.append(f"<b>✅ Рекомендации</b>\n{_bullets(report.recommendations)}")
    if report.content_ideas:
        tail.append(f"<b>💡 Идеи роликов</b>\n{_bullets(report.content_ideas)}")
    if tail:
        messages.append("\n\n".join(tail))
    return messages


def single_report(report: SingleReport, video: Video) -> list[str]:
    messages = [video_analysis(report.video, video)]
    tail = []
    if report.how_to_adapt:
        tail.append(f"<b>🔧 Как применить у себя</b>\n{_bullets(report.how_to_adapt)}")
    if report.content_ideas:
        tail.append(f"<b>💡 Идеи по мотивам</b>\n{_bullets(report.content_ideas)}")
    if tail:
        messages.append("\n\n".join(tail))
    return messages


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line breaks; tags in this module never span lines, so HTML stays valid."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            amp = line.rfind("&", cut - 10, cut)
            if amp != -1 and ";" not in line[amp:cut]:
                cut = amp  # do not cut an HTML entity in half
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
