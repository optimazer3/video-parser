"""Telegram handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from . import costs
from . import formatting as fmt
from .analyzer import AnalysisError
from .apify_source import ApifyError
from .links import find_url, parse_handle, resolve
from .models import INSTAGRAM, PLATFORM_LABELS, TIKTOK
from .service import Services

log = logging.getLogger(__name__)
router = Router()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

BTN_FEED = "🔥 Подборка"
BTN_PROFILE = "📊 Разбор аккаунта"
BTN_TOPICS = "⚙️ Темы"
BTN_HELP = "❓ Помощь"
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_FEED), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_TOPICS), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

HELP = f"""<b>Что я умею</b>

<b>{BTN_FEED}</b> (/feed) — свежие залетающие ролики из TikTok и Reels по вашим темам. \
Под каждым роликом есть кнопка «Почему залетело».

<b>Разбор аккаунта</b> — пришлите ссылку на профиль:
• tiktok.com/@username
• instagram.com/username
Я соберу последние ролики, найду залетевшие (×2 к медиане просмотров и выше), \
посмотрю кадры и речь и объясню, почему они зашли и что улучшить.

<b>Разбор одного ролика</b> — пришлите ссылку на видео TikTok или Reels.

<b>Темы</b> (/topics) — по чему искать подборку:
• /topics — показать текущие
• /topics #нейросети, #chatgpt, нейросети для работы — задать свои \
(с # — хэштег, без # — поисковый запрос)
• /topics reset — вернуть темы по умолчанию"""


class AccessMiddleware(BaseMiddleware):
    """Only the owner (ALLOWED_USER_IDS) may spend the Apify and Claude credits."""

    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and user.id in self.allowed:
            return await handler(event, data)
        if isinstance(event, Message) and user:
            text = f"⛔️ Доступ закрыт.\nВаш Telegram ID: <code>{user.id}</code>"
            if not self.allowed:
                text += "\n\nДобавьте его в ALLOWED_USER_IDS в файле .env и перезапустите бота."
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ закрыт", show_alert=True)
        return None


class StatusMessage:
    """One message that shows the progress of a long task."""

    def __init__(self, message: Message):
        self.message = message

    @classmethod
    async def create(cls, message: Message, text: str) -> "StatusMessage":
        return cls(await message.answer(text))

    async def update(self, text: str) -> None:
        try:
            await self.message.edit_text(text)
        except TelegramBadRequest as exc:
            log.debug("status edit failed: %s", exc)

    async def delete(self) -> None:
        try:
            await self.message.delete()
        except TelegramBadRequest:
            pass


async def send_long(message: Message, texts: list[str], **kwargs: Any) -> None:
    for text in texts:
        for chunk in fmt.split_message(text):
            await message.answer(chunk, **kwargs)


def video_button(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧠 Почему залетело", callback_data=f"an:{key}"),
    ]])


async def _no_progress(_: str) -> None:
    return None


async def send_feed(bot: Bot, chat_id: int, user_id: int, svc: Services, *, quiet: bool = False) -> None:
    status = None if quiet else StatusMessage(await bot.send_message(chat_id, "🔎 Ищу ролики…"))
    result = await svc.feed(user_id, status.update if status else _no_progress)
    if status:
        await status.delete()

    for warning in result.warnings:
        await bot.send_message(chat_id, f"⚠️ {escape(warning)}")
    checked = (f"Проверено роликов: {result.checked}, из них свежих "
               f"(за {svc.settings.feed_days} дн.): {result.fresh}.")
    if not result.videos:
        if not quiet:
            await bot.send_message(
                chat_id,
                f"Новых роликов, подходящих под фильтры, не нашлось. {checked} Попробуйте другие темы "
                "(/topics) или уменьшите FEED_MIN_VIEWS.",
            )
        return

    await bot.send_message(chat_id, f"<b>🔥 Подборка — {len(result.videos)}</b>\n<i>{checked}</i>")
    for i, video in enumerate(result.videos, 1):
        await bot.send_message(chat_id, fmt.feed_card(video, i), reply_markup=video_button(video.key))
    await svc.storage.mark_seen(user_id, [v.key for v in result.videos])


async def run_exclusive(message: Message, svc: Services, user_id: int, job: Callable[[], Awaitable[None]]) -> None:
    lock = svc.user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Я ещё выполняю предыдущую задачу — дождитесь результата.")
        return
    async with lock:
        spent = costs.track()
        try:
            await job()
        except (ApifyError, AnalysisError) as exc:
            await message.answer(f"❌ {escape(str(exc))}")
        except Exception as exc:  # noqa: BLE001 - report anything to the user instead of failing silently
            log.exception("job failed")
            await message.answer(f"❌ Что-то пошло не так: {escape(type(exc).__name__)}: {escape(str(exc))[:500]}")
        if not spent.empty:
            await message.answer(fmt.costs_line(spent))


async def analyze_profile(message: Message, svc: Services, user_id: int, platform: str, username: str) -> None:
    async def job() -> None:
        status = await StatusMessage.create(message, "⏳ Начинаю…")
        result = await svc.analyze_profile(platform, username, status.update)
        await status.delete()
        await send_long(message, [fmt.profile_overview(platform, username, result.stats)],
                        link_preview_options=NO_PREVIEW)
        if result.report is None:
            await message.answer("ℹ️ POLZA_API_KEY не задан — показываю только статистику, без разбора.")
            return
        await send_long(message, fmt.profile_report(result.report, result.stats), link_preview_options=NO_PREVIEW)

    await run_exclusive(message, svc, user_id, job)


async def analyze_video(message: Message, svc: Services, user_id: int, **kwargs: Any) -> None:
    async def job() -> None:
        if svc.analyzer is None:
            await message.answer("ℹ️ Для разбора роликов нужен POLZA_API_KEY в .env.")
            return
        status = await StatusMessage.create(message, "⏳ Начинаю…")
        video, report = await svc.analyze_video(progress=status.update, **kwargs)
        await status.delete()
        await send_long(message, fmt.single_report(report, video), link_preview_options=NO_PREVIEW)

    await run_exclusive(message, svc, user_id, job)


# --- Commands and buttons ---------------------------------------------------

@router.message(CommandStart())
@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_start(message: Message) -> None:
    await message.answer(HELP, reply_markup=MENU, link_preview_options=NO_PREVIEW)


@router.message(Command("feed"))
@router.message(F.text == BTN_FEED)
async def cmd_feed(message: Message, bot: Bot, svc: Services) -> None:
    user_id = message.from_user.id
    await run_exclusive(message, svc, user_id, lambda: send_feed(bot, message.chat.id, user_id, svc))


@router.message(F.text == BTN_PROFILE)
async def cmd_profile_hint(message: Message) -> None:
    await message.answer(
        "Пришлите ссылку на профиль (tiktok.com/@username или instagram.com/username) "
        "или на конкретный ролик.",
        link_preview_options=NO_PREVIEW,
    )


@router.message(Command("topics"))
@router.message(F.text == BTN_TOPICS)
async def cmd_topics(message: Message, svc: Services, command: CommandObject | None = None) -> None:
    user_id = message.from_user.id
    args = (command.args or "").strip() if command else ""
    if args.lower() == "reset":
        await svc.storage.set_topics(user_id, None)
    elif args:
        topics = [t.strip() for t in args.replace("\n", ",").split(",") if t.strip()]
        await svc.storage.set_topics(user_id, topics)
    topics = await svc.storage.get_topics(user_id, svc.settings.default_topics)
    await message.answer(
        "<b>Темы подборки</b>\n" + "\n".join(f"• {escape(t)}" for t in topics)
        + "\n\nИзменить: <code>/topics #нейросети, #chatgpt, нейросети для работы</code>\n"
        "С # — хэштег, без # — поисковый запрос. Сбросить: <code>/topics reset</code>"
    )


@router.callback_query(F.data.startswith("an:"))
async def cb_analyze_video(callback: CallbackQuery, svc: Services) -> None:
    key = callback.data.removeprefix("an:")
    video = await svc.storage.get_video(key)
    await callback.answer()
    if video is None:
        await callback.message.answer("Этот ролик уже не в кэше — пришлите ссылку на него сообщением.")
        return
    await analyze_video(callback.message, svc, callback.from_user.id, video=video)


@router.callback_query(F.data.startswith("pf:"))
async def cb_pick_platform(callback: CallbackQuery, svc: Services) -> None:
    _, platform, username = callback.data.split(":", 2)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await analyze_profile(callback.message, svc, callback.from_user.id, platform, username)


@router.message(F.text)
async def on_text(message: Message, svc: Services) -> None:
    user_id = message.from_user.id
    url = find_url(message.text)
    if url:
        try:
            target = await resolve(url, svc.settings.proxy)
        except Exception:  # noqa: BLE001
            log.exception("resolve failed")
            target = None
        if target is None:
            await message.answer("Не понял ссылку. Нужен профиль или ролик TikTok / Instagram.")
        elif target.kind == "profile":
            await analyze_profile(message, svc, user_id, target.platform, target.username)
        else:
            await analyze_video(message, svc, user_id, platform=target.platform, url=target.url)
        return

    handle = parse_handle(message.text)
    if handle:
        await message.answer(
            f"Где искать @{escape(handle)}?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=PLATFORM_LABELS[TIKTOK], callback_data=f"pf:{TIKTOK}:{handle}"),
                InlineKeyboardButton(text="Instagram", callback_data=f"pf:{INSTAGRAM}:{handle}"),
            ]]),
        )
        return

    await message.answer("Пришлите ссылку на профиль или ролик, либо выберите действие в меню.", reply_markup=MENU)
