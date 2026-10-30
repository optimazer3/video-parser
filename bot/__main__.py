"""Entry point: python -m bot"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand

from . import costs
from . import formatting as fmt
from .config import Settings
from .handlers import AccessMiddleware, router, send_feed
from .net import safe_proxy
from .service import Services
from .storage import Storage

log = logging.getLogger("bot")

NO_TELEGRAM = """Не удаётся подключиться к Telegram (api.telegram.org): %s

Похоже, Telegram заблокирован у вашего провайдера. Что сделать:
  1) включите VPN на весь компьютер (не только в браузере) и запустите бота снова;
  2) или впишите в .env прокси вашего VPN-клиента, например:
       PROXY=socks5://127.0.0.1:10808
     (адрес и порт смотрите в настройках клиента: «локальный прокси», «SOCKS5», «mixed port»)."""

NO_TELEGRAM_VIA_PROXY = """Не удаётся подключиться к Telegram через прокси %s: %s

Проверьте, что VPN-клиент запущен, а адрес и порт в PROXY в .env совпадают с его настройками."""


async def check_telegram(bot: Bot, proxy: str | None) -> bool:
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram не принял TELEGRAM_BOT_TOKEN — проверьте токен от @BotFather в .env")
        return False
    except TelegramNetworkError as exc:
        if proxy:
            log.error(NO_TELEGRAM_VIA_PROXY, safe_proxy(proxy), exc.message)
        else:
            log.error(NO_TELEGRAM, exc.message)
        return False
    log.info("Подключился к Telegram как @%s%s", me.username, f" через прокси {safe_proxy(proxy)}" if proxy else "")
    return True


def seconds_until(at: dtime, tz: ZoneInfo, now: datetime | None = None) -> float:
    now = now or datetime.now(tz)
    target = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_digest(bot: Bot, svc: Services) -> None:
    hour, minute = map(int, svc.settings.digest_time.split(":"))
    at, tz = dtime(hour, minute), ZoneInfo(svc.settings.timezone)
    while True:
        await asyncio.sleep(seconds_until(at, tz))
        for user_id in svc.settings.allowed_user_ids:
            lock = svc.user_lock(user_id)
            if lock.locked():
                continue
            async with lock:
                spent = costs.track()
                try:
                    await send_feed(bot, user_id, user_id, svc, quiet=True)
                    if not spent.empty:
                        await bot.send_message(user_id, fmt.costs_line(spent))
                except Exception:  # noqa: BLE001 - the digest must keep running tomorrow
                    log.exception("digest failed for %s", user_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS пуст: бот ответит только вашим Telegram ID. Напишите ему /start.")
    log.info("Настройки из %s", settings.env_path or "переменных окружения (файл .env не найден)")
    for key in settings.env_duplicates:
        log.warning("В .env ключ %s записан несколько раз — действует последнее значение. Оставьте одну строку.", key)
    if settings.llm_api_key:
        log.info("Модель разбора: %s · расшифровка речи: %s", settings.llm_model, settings.transcribe_model or "выключена")
    else:
        log.warning("POLZA_API_KEY не задан: будет только статистика, без разбора роликов.")

    session = AiohttpSession(proxy=settings.proxy) if settings.proxy else None
    bot = Bot(settings.telegram_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if not await check_telegram(bot, settings.proxy):
        await bot.session.close()
        raise SystemExit(1)

    storage = Storage(settings.db_path)
    await storage.open()
    svc = Services(settings, storage)

    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.callback_query.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.include_router(router)

    digest = asyncio.create_task(daily_digest(bot, svc)) if settings.digest_time else None
    try:
        await bot.set_my_commands([
            BotCommand(command="feed", description="Подборка залетающих роликов"),
            BotCommand(command="topics", description="Темы подборки"),
            BotCommand(command="help", description="Что умеет бот"),
        ])
        log.info("Бот запущен. Остановить — Ctrl+C")
        await dp.start_polling(bot, svc=svc)
    finally:
        if digest:
            digest.cancel()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
