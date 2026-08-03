import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

import config
from database import DbSessionMiddleware, init_db
from handlers import router
from middlewares import ForceSubMiddleware
from texts import LOG_BOT_RUNNING


from aiogram.client.session.aiohttp import AiohttpSession

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    os.makedirs(config.TEMP_DIR, exist_ok=True)

    # 1. Baza va jadvallarni ishga tushirish
    await init_db()

    session = AiohttpSession(timeout=300)
    bot = Bot(config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()

    # 2. Database Session Middleware (outer middleware)
    db_middleware = DbSessionMiddleware()
    dp.message.outer_middleware(db_middleware)
    dp.callback_query.outer_middleware(db_middleware)

    # 3. Force Subscribe Middleware (outer middleware)
    force_sub_middleware = ForceSubMiddleware()
    dp.message.outer_middleware(force_sub_middleware)
    dp.callback_query.outer_middleware(force_sub_middleware)

    # 4. Router ulanishi
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info(LOG_BOT_RUNNING)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
