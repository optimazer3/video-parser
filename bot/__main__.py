"""Entry point: python -m bot"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import Settings
from .handlers import AccessMiddleware, router, send_feed
from .service import Services
from .storage import Storage

log = logging.getLogger("bot")


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
                try:
                    await send_feed(bot, user_id, user_id, svc, quiet=True)
                except Exception:  # noqa: BLE001 - the digest must keep running tomorrow
                    log.exception("digest failed for %s", user_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS пуст: бот ответит только вашим Telegram ID. Напишите ему /start.")
    if not settings.llm_api_key:
        log.warning("POLZA_API_KEY не задан: будет только статистика, без разбора роликов.")

    storage = Storage(settings.db_path)
    await storage.open()
    svc = Services(settings, storage)

    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.callback_query.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="feed", description="Подборка залетающих роликов"),
        BotCommand(command="topics", description="Темы подборки"),
        BotCommand(command="help", description="Что умеет бот"),
    ])

    digest = asyncio.create_task(daily_digest(bot, svc)) if settings.digest_time else None
    try:
        log.info("Бот запущен")
        await dp.start_polling(bot, svc=svc)
    finally:
        if digest:
            digest.cancel()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
