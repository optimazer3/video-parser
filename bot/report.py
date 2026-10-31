"""The full account breakdown as a file (PDF, or Markdown when no Cyrillic font is found)."""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .analyzer import ProfileReport, VideoAnalysis
from .formatting import MAX_IDEAS, MAX_IMPROVE, MAX_STEPS, MAX_TAKEAWAYS, clean_items, num
from .media import VideoMedia
from .metrics import ProfileStats, estimated_views
from .models import PLATFORM_LABELS, Video

log = logging.getLogger(__name__)

ACCENT = (108, 92, 231)
TEXT = (33, 33, 41)
MUTED = (120, 120, 135)
BOX = (243, 241, 252)
HOOK_FRAMES = 3  # frames from the first seconds shown under each video

# Emoji and pictographs are not in ordinary fonts: drop them from the PDF instead of printing empty boxes.
NO_GLYPH_RE = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D\u20E3]")


def _font_candidates() -> list[tuple[str, str]]:
    windows = os.environ.get("WINDIR", r"C:\Windows")
    return [
        (os.environ.get("PDF_FONT", ""), os.environ.get("PDF_FONT_BOLD", "")),
        (rf"{windows}\Fonts\segoeui.ttf", rf"{windows}\Fonts\segoeuib.ttf"),
        (rf"{windows}\Fonts\arial.ttf", rf"{windows}\Fonts\arialbd.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ]


def find_fonts() -> tuple[str, str] | None:
    for regular, bold in _font_candidates():
        if regular and Path(regular).is_file():
            return regular, bold if bold and Path(bold).is_file() else regular
    return None


def _text(value: str) -> str:
    return NO_GLYPH_RE.sub("", value or "").strip()


def file_name(platform: str, username: str, extension: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", username) or "account"
    return f"razbor_{safe}_{platform}_{datetime.now():%Y-%m-%d}.{extension}"


def _video_for(analysis: VideoAnalysis, stats: ProfileStats) -> Video | None:
    index = analysis.video_number - 1
    return stats.hits[index] if 0 <= index < len(stats.hits) else None


def _stat_tiles(stats: ProfileStats) -> list[tuple[str, str]]:
    tiles = [("Роликов", str(len(stats.videos))), ("Медиана просмотров", num(stats.median_views))]
    if stats.mean_er is not None:
        tiles.append(("Средний ER", f"{stats.mean_er * 100:.1f}%"))
    tiles.append(("Залетевших (×2+)", str(stats.hit_count)))
    if stats.posts_per_week:
        tiles.append(("Постов в неделю", f"{stats.posts_per_week:.1f}"))
    return tiles


# --- PDF ----------------------------------------------------------------------


def build_pdf(
    platform: str, username: str, stats: ProfileStats, report: ProfileReport,
    media: dict[str, VideoMedia], footer: str = "",
) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    fonts = find_fonts()
    if fonts is None:
        raise RuntimeError("не найден шрифт с кириллицей (задайте PDF_FONT в .env)")

    class ReportPDF(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("Body", size=8)
            self.set_text_color(*MUTED)
            self.cell(0, 5, f"@{username} · {PLATFORM_LABELS.get(platform, platform)} · стр. {self.page_no()}", align="C")

    pdf = ReportPDF(format="A4")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, 18)
    pdf.add_font("Body", "", fonts[0])
    pdf.add_font("Body", "B", fonts[1])
    width = pdf.epw

    def line(text: str, size: float = 10.5, bold: bool = False, color=TEXT, height: float = 5.6, link: str = "") -> None:
        pdf.set_font("Body", "B" if bold else "", size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, height, _text(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT, link=link)

    def heading(text: str, size: float = 14) -> None:
        if pdf.get_y() > pdf.page_break_trigger - 30:
            pdf.add_page()
        pdf.ln(3)
        line(text, size=size, bold=True, color=ACCENT, height=7.5)
        pdf.ln(1)

    def label(text: str) -> None:
        pdf.ln(1.5)
        line(text, size=10, bold=True, color=MUTED, height=5.2)

    def items(values: list[str], numbered: bool = False) -> None:
        for i, value in enumerate(values, 1):
            marker = f"{i}." if numbered else "•"
            pdf.set_font("Body", "", 10.5)
            pdf.set_text_color(*ACCENT)
            pdf.cell(7, 5.6, marker)
            pdf.set_text_color(*TEXT)
            pdf.multi_cell(0, 5.6, _text(value), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def boxed(title: str, text: str) -> None:
        pdf.ln(1.5)
        pdf.set_fill_color(*BOX)
        pdf.set_font("Body", "B", 9.5)
        pdf.set_text_color(*ACCENT)
        pdf.multi_cell(0, 6, "  " + title, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Body", "", 10)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(0, 5.4, _text(text), fill=True, align="L", padding=(0, 3, 2.5, 3),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Title and numbers
    pdf.add_page()
    line(f"Разбор аккаунта @{username}", size=22, bold=True, height=10)
    line(f"{PLATFORM_LABELS.get(platform, platform)} · {datetime.now():%d.%m.%Y}", size=10.5, color=MUTED)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin, pdf.get_y() + 2, pdf.l_margin + 30, pdf.get_y() + 2)
    pdf.ln(6)
    tiles = _stat_tiles(stats)
    tile_w = (width - 3 * (len(tiles) - 1)) / len(tiles)
    y = pdf.get_y()
    for i, (name, value) in enumerate(tiles):
        x = pdf.l_margin + i * (tile_w + 3)
        pdf.set_fill_color(*BOX)
        pdf.rect(x, y, tile_w, 17, style="F")
        pdf.set_xy(x, y + 2.5)
        pdf.set_font("Body", "B", 14)
        pdf.set_text_color(*ACCENT)
        pdf.cell(tile_w, 7, value, align="C")
        pdf.set_xy(x, y + 10)
        pdf.set_font("Body", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(tile_w, 4, name, align="C")
    pdf.set_xy(pdf.l_margin, y + 21)

    heading("Главное")
    line(report.summary)
    takeaways = clean_items(report.takeaways, MAX_TAKEAWAYS)
    if takeaways:
        pdf.ln(1)
        items(takeaways)

    heading("Лучшие ролики")
    for i, video in enumerate(stats.hits, 1):
        pdf.set_font("Body", "B", 10.5)
        pdf.set_text_color(*TEXT)
        pdf.cell(9, 6, f"{i}.")
        pdf.cell(30, 6, f"{num(estimated_views(video))} просм.")
        pdf.set_text_color(*ACCENT)
        pdf.cell(42, 6, f"×{stats.ratio(video):.1f} к медиане")
        pdf.set_font("Body", "", 10)
        pdf.set_text_color(*TEXT)
        caption = _text(" ".join(video.caption.split())) or "без подписи"
        room = pdf.l_margin + width - pdf.get_x()
        if pdf.get_string_width(caption) > room:
            while caption and pdf.get_string_width(caption + "…") > room:
                caption = caption[:-1]
            caption = caption.rstrip() + "…"
        pdf.cell(0, 6, caption, link=video.url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # One section per analysed video; a new page only when the section would not start comfortably
    for analysis in report.videos:
        video = _video_for(analysis, stats)
        if pdf.get_y() > pdf.page_break_trigger - 110:
            pdf.add_page()
        else:
            pdf.ln(5)
            pdf.set_draw_color(225, 222, 240)
            pdf.set_line_width(0.3)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            pdf.ln(5)
        title = f"Ролик №{analysis.video_number}"
        if video:
            title += f" · {num(estimated_views(video))} просмотров · ×{stats.ratio(video):.1f} к медиане"
        line(title, size=15, bold=True, color=ACCENT, height=8)
        if video:
            line(video.url, size=8.5, color=MUTED, height=4.5, link=video.url)
            if video.caption.strip():
                line("«" + " ".join(video.caption.split())[:220] + "»", size=9.5, color=MUTED, height=5)

        frames = (media.get(video.key).frames if video and media.get(video.key) else [])[:HOOK_FRAMES]
        if frames:
            pdf.ln(2)
            frame_w, gap = 34, 4
            frame_h = frame_w * 16 / 9
            if pdf.get_y() + frame_h + 8 > pdf.page_break_trigger:
                pdf.add_page()
            y = pdf.get_y()
            for i, (second, jpeg) in enumerate(frames):
                x = pdf.l_margin + i * (frame_w + gap)
                try:
                    pdf.image(io.BytesIO(jpeg), x=x, y=y, w=frame_w, h=frame_h, keep_aspect_ratio=True)
                except Exception as exc:  # noqa: BLE001 - a broken frame should not break the report
                    log.warning("frame not drawn: %s", exc)
                pdf.set_xy(x, y + frame_h + 0.5)
                pdf.set_font("Body", "", 8)
                pdf.set_text_color(*MUTED)
                pdf.cell(frame_w, 4, f"{second:g} с", align="C")
            pdf.set_xy(pdf.l_margin, y + frame_h + 6)

        label("ПОЧЕМУ ЗАЛЕТЕЛ")
        line(analysis.why)
        label("ХУК")
        line(analysis.hook)
        steps = clean_items(analysis.how_to_make, MAX_STEPS)
        if steps:
            label("КАК СДЕЛАТЬ БЕЗ ЛИЦА")
            items(steps, numbered=True)
        if analysis.key_prompt.strip():
            boxed("Промпт для Claude / Gemini", analysis.key_prompt)
        improve = clean_items(analysis.improve, MAX_IMPROVE)
        if improve:
            label("УЛУЧШИТЬ")
            items(improve)

    ideas = clean_items(report.ideas, MAX_IDEAS)
    if ideas:
        heading("Идеи роликов")
        items(ideas, numbered=True)
    if footer:
        pdf.ln(6)
        line(footer, size=8.5, color=MUTED, height=4.5)
    return bytes(pdf.output())


# --- Markdown -------------------------------------------------------------------


def build_markdown(platform: str, username: str, stats: ProfileStats, report: ProfileReport, footer: str = "") -> str:
    out = [f"# Разбор аккаунта @{username}", "",
           f"{PLATFORM_LABELS.get(platform, platform)} · {datetime.now():%d.%m.%Y}", ""]
    out += [" · ".join(f"**{name}:** {value}" for name, value in _stat_tiles(stats)), ""]
    out += ["## Главное", "", report.summary, ""]
    out += [f"- {t}" for t in clean_items(report.takeaways, MAX_TAKEAWAYS)] + [""]
    out += ["## Лучшие ролики", ""]
    for i, video in enumerate(stats.hits, 1):
        caption = " ".join(video.caption.split())[:70] or "без подписи"
        out.append(f"{i}. [{caption}]({video.url}) — {num(estimated_views(video))} просм., ×{stats.ratio(video):.1f}")
    out.append("")
    for analysis in report.videos:
        video = _video_for(analysis, stats)
        title = f"## Ролик №{analysis.video_number}"
        if video:
            title += f" · {num(estimated_views(video))} просмотров · ×{stats.ratio(video):.1f} к медиане"
        out += [title, ""]
        if video:
            out += [video.url, ""]
        out += [f"**Почему залетел.** {analysis.why}", "", f"**Хук.** {analysis.hook}", ""]
        steps = clean_items(analysis.how_to_make, MAX_STEPS)
        if steps:
            out += ["**Как сделать без лица:**", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)] + [""]
        if analysis.key_prompt.strip():
            out += ["**Промпт для Claude / Gemini:**", "", "```", analysis.key_prompt.strip(), "```", ""]
        improve = clean_items(analysis.improve, MAX_IMPROVE)
        if improve:
            out += ["**Улучшить:**", ""] + [f"- {s}" for s in improve] + [""]
    ideas = clean_items(report.ideas, MAX_IDEAS)
    if ideas:
        out += ["## Идеи роликов", ""] + [f"{i}. {s}" for i, s in enumerate(ideas, 1)] + [""]
    if footer:
        out += ["---", "", footer, ""]
    return "\n".join(out)
