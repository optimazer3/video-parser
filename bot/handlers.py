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
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
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
from . import report as report_file
from . import topics as topic_list
from .analyzer import AnalysisError
from .apify_source import ApifyError, feed_cost_usd
from .catalog import SUGGESTED
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

<b>Темы</b> (/topics) — по чему искать подборку (с # — хэштег, без # — поисковый запрос):
• /topics — показать, удалить кнопкой ❌ или добавить кнопкой ➕
• /topics + #sora, нейросети для монтажа — добавить к текущим
• /topics - #sora — удалить
• /topics #нейросети, #chatgpt — заменить весь список
• /topics reset — вернуть темы по умолчанию

<b>Нейросеть</b> (/model) — какая модель разбирает ролики, сколько стоит разбор, \
режим «видео целиком» (модель смотрит ролик со звуком, а не 6 кадров).
• /model provider/name — выбрать любую модель из каталога Polza
• /model reset — вернуть LLM_MODEL из .env"""


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
        if not spent.empty and not spent.shown:
            await message.answer(fmt.costs_line(spent))


async def analyze_profile(message: Message, svc: Services, user_id: int, platform: str, username: str) -> None:
    async def job() -> None:
        status = await StatusMessage.create(message, "⏳ Начинаю…")
        result = await svc.analyze_profile(user_id, platform, username, status.update)
        await status.delete()
        if result.report is None or svc.settings.report_format == "chat":
            await send_long(message, [fmt.profile_overview(platform, username, result.stats)],
                            link_preview_options=NO_PREVIEW)
            if result.report is None:
                await message.answer("ℹ️ POLZA_API_KEY не задан — показываю только статистику, без разбора.")
            else:
                await send_long(message, fmt.profile_report(result.report, result.stats), link_preview_options=NO_PREVIEW)
            return
        await send_report_file(message, svc, platform, username, result)

    await run_exclusive(message, svc, user_id, job)


async def send_report_file(message: Message, svc: Services, platform: str, username: str, result: Any) -> None:
    """The whole breakdown as one PDF (or Markdown) with a short caption, so the chat stays clean."""
    spent = costs.current()
    footer = fmt.costs_plain(spent, result.model)
    data, name = None, None
    if svc.settings.report_format != "md":
        try:
            data = report_file.build_pdf(platform, username, result.stats, result.report, result.media, footer)
            name = report_file.file_name(platform, username, "pdf")
        except Exception as exc:  # noqa: BLE001 - fall back to Markdown rather than lose the report
            log.exception("PDF report failed")
            costs.add_note(f"PDF не собрался ({str(exc)[:120]}) — отправил Markdown")
    if data is None:
        data = report_file.build_markdown(platform, username, result.stats, result.report, footer).encode("utf-8")
        name = report_file.file_name(platform, username, "md")
    caption = fmt.report_caption(platform, username, result.stats, result.report, spent)
    await message.answer_document(BufferedInputFile(data, filename=name), caption=caption)
    if spent is not None:
        spent.shown = True


async def analyze_video(message: Message, svc: Services, user_id: int, **kwargs: Any) -> None:
    async def job() -> None:
        if svc.analyzer is None:
            await message.answer("ℹ️ Для разбора роликов нужен POLZA_API_KEY в .env.")
            return
        status = await StatusMessage.create(message, "⏳ Начинаю…")
        video, report = await svc.analyze_video(user_id, progress=status.update, **kwargs)
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


TOPICS_ADD_PROMPT = "➕ Новые темы"


async def topics_view(svc: Services, user_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    topics = await svc.storage.get_topics(user_id, svc.settings.default_topics)
    s = svc.settings
    estimate = feed_cost_usd(topics, s.feed_results_per_topic, s.feed_platforms)
    lines = [f"<i>{escape(note)}</i>\n"] if note else []
    lines.append(f"<b>Темы подборки</b> · Apify ≈ ${estimate:.2f} за подборку")
    lines += [f"• {escape(t)}" for t in topics]
    lines.append(
        "\nДобавить: <code>/topics + #sora, нейросети для монтажа</code>\n"
        "Удалить: кнопка ❌ или <code>/topics - #sora</code>\n"
        "С # — хэштег, без # — поисковый запрос."
    )
    buttons = [InlineKeyboardButton(text=f"❌ {t}"[:60], callback_data=f"tp:del:{i}") for i, t in enumerate(topics)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="tp:add"),
        InlineKeyboardButton(text="↩️ По умолчанию", callback_data="tp:reset"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_topics(callback: CallbackQuery, svc: Services, note: str) -> None:
    text, keyboard = await topics_view(svc, callback.from_user.id, note)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=keyboard)


@router.message(Command("topics"))
@router.message(F.text == BTN_TOPICS)
async def cmd_topics(message: Message, svc: Services, command: CommandObject | None = None) -> None:
    user_id = message.from_user.id
    args = (command.args or "").strip() if command else ""
    note = ""
    if args:
        current = await svc.storage.get_topics(user_id, svc.settings.default_topics)
        updated, note = topic_list.apply_command(current, args)
        if updated != current:
            await svc.storage.set_topics(user_id, updated)
    text, keyboard = await topics_view(svc, user_id, note)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("tp:del:"))
async def cb_topic_delete(callback: CallbackQuery, svc: Services) -> None:
    user_id = callback.from_user.id
    topics = await svc.storage.get_topics(user_id, svc.settings.default_topics)
    index = int(callback.data.rsplit(":", 1)[1])
    if index >= len(topics):
        await callback.answer("Список уже изменился", show_alert=True)
        return await show_topics(callback, svc, "")
    if len(topics) == 1:
        await callback.answer("Нельзя удалить последнюю тему", show_alert=True)
        return None
    removed = topics.pop(index)
    await svc.storage.set_topics(user_id, topics)
    await callback.answer(f"Удалил {removed}")
    await show_topics(callback, svc, f"Удалил: {removed}")


@router.callback_query(F.data == "tp:reset")
async def cb_topic_reset(callback: CallbackQuery, svc: Services) -> None:
    await svc.storage.set_topics(callback.from_user.id, None)
    await callback.answer()
    await show_topics(callback, svc, "Вернул темы по умолчанию.")


@router.callback_query(F.data == "tp:add")
async def cb_topic_add(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        f"{TOPICS_ADD_PROMPT}: напишите в ответ через запятую. С # — хэштег, без # — поисковый запрос.",
        reply_markup=ForceReply(input_field_placeholder="#sora, нейросети для монтажа"),
    )


@router.message(F.reply_to_message.text.startswith(TOPICS_ADD_PROMPT), F.text)
async def on_topics_reply(message: Message, svc: Services) -> None:
    user_id = message.from_user.id
    current = await svc.storage.get_topics(user_id, svc.settings.default_topics)
    updated, added = topic_list.add(current, topic_list.parse(message.text))
    if added:
        await svc.storage.set_topics(user_id, updated)
    note = "Добавил: " + ", ".join(added) if added else "Эти темы уже есть."
    text, keyboard = await topics_view(svc, user_id, note)
    await message.answer(text, reply_markup=keyboard)


def _rub(value: float | None) -> str:
    return f"≈ {value:.1f} ₽" if value is not None else "цена неизвестна"


async def model_view(svc: Services, user_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    current = await svc.model_for(user_id)
    custom = await svc.storage.get_setting(user_id, "model") is not None
    video_on = await svc.video_enabled(user_id)
    info = await svc.catalog.get(current)

    lines = [f"<i>{escape(note)}</i>\n"] if note else []
    lines.append(f"<b>Модель разбора:</b> <code>{escape(current)}</code> ({'выбрана в боте' if custom else 'из .env'})")
    if info:
        price = f"Разбор аккаунта: {_rub(info.estimate_rub())}"
        if info.video and video_on:
            price += f", с видео целиком {_rub(info.estimate_rub(with_video=True))}"
        lines.append(price)
        lines.append("Видео целиком: " + ("умеет" if info.video else "не умеет — разбирает по кадрам"))
    elif svc.catalog.loaded:
        lines.append("⚠️ Такой модели нет в каталоге Polza — проверьте название.")
    lines.append(f"Режим «видео целиком»: {'включён' if video_on else 'выключен'}")

    lines.append("\n<b>Варианты</b> (цена разбора аккаунта):")
    for model_id, label in SUGGESTED:
        option = await svc.catalog.get(model_id)
        lines.append(f"• <code>{model_id}</code> — {label}, {_rub(option.estimate_rub() if option else None)}")
    lines.append("\nДругая модель: <code>/model provider/name</code> — список: polza.ai/api/v1/models")

    rows = [[InlineKeyboardButton(text=("✅ " if model_id == current else "") + model_id, callback_data=f"md:set:{model_id}")]
            for model_id, _ in SUGGESTED]
    rows.append([
        InlineKeyboardButton(text=f"🎞 Видео целиком: {'вкл' if video_on else 'выкл'}", callback_data="md:video"),
        InlineKeyboardButton(text="↩️ Из .env", callback_data="md:reset"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def choose_model(svc: Services, user_id: int, model_id: str) -> str:
    """Save the model if it fits the job; returns a note for the user."""
    info = await svc.catalog.get(model_id)
    if svc.catalog.loaded and info is None:
        return f"Модели {model_id} нет в каталоге Polza — модель не изменена."
    if info and not info.images:
        return f"{model_id} не понимает картинки, а разбор строится на кадрах — модель не изменена."
    await svc.storage.set_setting(user_id, "model", model_id)
    return f"Теперь разбирает {model_id}."


@router.message(Command("model"))
async def cmd_model(message: Message, svc: Services, command: CommandObject) -> None:
    user_id = message.from_user.id
    args = (command.args or "").strip()
    note = ""
    if args.lower() in ("reset", "сброс"):
        await svc.storage.set_setting(user_id, "model", None)
        note = f"Вернул модель из .env: {svc.settings.llm_model}."
    elif args:
        note = await choose_model(svc, user_id, args.split()[0])
    text, keyboard = await model_view(svc, user_id, note)
    await message.answer(text, reply_markup=keyboard, link_preview_options=NO_PREVIEW)


async def show_model(callback: CallbackQuery, svc: Services, note: str) -> None:
    text, keyboard = await model_view(svc, callback.from_user.id, note)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=keyboard, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("md:"))
async def cb_model(callback: CallbackQuery, svc: Services) -> None:
    user_id = callback.from_user.id
    action = callback.data.removeprefix("md:")
    if action.startswith("set:"):
        note = await choose_model(svc, user_id, action.removeprefix("set:"))
    elif action == "video":
        enabled = not await svc.video_enabled(user_id)
        await svc.storage.set_setting(user_id, "video", "on" if enabled else "off")
        note = "Режим «видео целиком» " + ("включён: работает с моделями, которые умеют видео." if enabled else "выключен.")
    else:
        await svc.storage.set_setting(user_id, "model", None)
        note = f"Вернул модель из .env: {svc.settings.llm_model}."
    await callback.answer()
    await show_model(callback, svc, note)


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
