"""Telegram message texts (HTML parse mode)."""

from __future__ import annotations

import re
from html import escape

from .analyzer import ProfileReport, SingleReport, VideoAnalysis
from .costs import Costs
from .metrics import ProfileStats, estimated_views
from .models import PLATFORM_LABELS, TIKTOK, Video

TELEGRAM_LIMIT = 4096
PLATFORM_ICONS = {TIKTOK: "🎵", "instagram": "📸"}


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(4, "тема", "темы", "тем") -> "4 темы"."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


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


# How much of each list the report shows: the model is asked for this, the formatter enforces it.
MAX_STEPS, MAX_IMPROVE, MAX_TAKEAWAYS, MAX_IDEAS = 5, 2, 3, 3
STEP_NUMBER_RE = re.compile(r"^\s*(?:шаг\s*)?\d+\s*[.):—-]\s*", re.IGNORECASE)


def _clean(items: list[str], limit: int) -> list[str]:
    return [STEP_NUMBER_RE.sub("", item).strip() for item in items if item.strip()][:limit]


def _bullets(items: list[str], limit: int) -> str:
    return "\n".join(f"• {escape(item)}" for item in _clean(items, limit))


def _steps(items: list[str], limit: int) -> str:
    return "\n".join(f"{i}. {escape(item)}" for i, item in enumerate(_clean(items, limit), 1))


def video_analysis(analysis: VideoAnalysis, video: Video | None, ratio: float | None = None) -> str:
    header = f"<b>🎬 Ролик №{analysis.video_number}</b>"
    if video:
        header += f" · {num(estimated_views(video))} просм."
        if ratio:
            header += f" · ×{ratio:.1f} к медиане"
        header += f"\n{escape(video.url)}"
    parts = [
        header,
        f"🔥 <b>Почему залетел:</b> {escape(analysis.why)}",
        f"🪝 <b>Хук:</b> {escape(analysis.hook)}",
    ]
    if _clean(analysis.how_to_make, MAX_STEPS):
        parts.append(f"🛠 <b>Как сделать без лица:</b>\n{_steps(analysis.how_to_make, MAX_STEPS)}")
    if _clean(analysis.improve, MAX_IMPROVE):
        parts.append(f"⚠️ <b>Улучшить:</b>\n{_bullets(analysis.improve, MAX_IMPROVE)}")
    return "\n\n".join(parts)


def _ideas(ideas: list[str]) -> str | None:
    return f"💡 <b>Идеи роликов</b>\n{_bullets(ideas, MAX_IDEAS)}" if _clean(ideas, MAX_IDEAS) else None


def pack(blocks: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Join short blocks into as few messages as possible without cutting a block in two."""
    messages: list[str] = []
    for block in filter(None, blocks):
        if messages and len(messages[-1]) + 2 + len(block) <= limit:
            messages[-1] += "\n\n" + block
        else:
            messages.append(block)
    return messages


def profile_report(report: ProfileReport, stats: ProfileStats) -> list[str]:
    head = f"🧠 <b>Главное</b>\n{escape(report.summary)}"
    if _clean(report.takeaways, MAX_TAKEAWAYS):
        head += "\n" + _bullets(report.takeaways, MAX_TAKEAWAYS)
    blocks = [head]
    for analysis in report.videos:
        index = analysis.video_number - 1
        video = stats.hits[index] if 0 <= index < len(stats.hits) else None
        blocks.append(video_analysis(analysis, video, stats.ratio(video) if video else None))
    blocks.append(_ideas(report.ideas))
    return pack(blocks)


def single_report(report: SingleReport, video: Video) -> list[str]:
    return pack([video_analysis(report.video, video), _ideas(report.ideas)])


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


def costs_line(costs: Costs) -> str:
    parts = []
    if costs.apify_runs:
        parts.append(f"Apify ≈ ${costs.apify_usd:.2f} ({len(costs.apify_runs)} запуск.)")
    elif costs.from_cache:
        parts.append("Apify — $0, профиль из кэша")
    if costs.llm_models:
        models = ", ".join(escape(m) for m in costs.llm_models)
        parts.append(f"нейросеть ≈ {costs.llm_rub:.1f} ₽ ({models})")
    lines = ["<i>💸 " + " · ".join(parts) + "</i>"] if parts else []
    lines += [f"<i>{escape(note)}</i>" for note in costs.notes]
    return "\n".join(lines)
