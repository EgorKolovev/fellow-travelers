import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import router
from app.config import get_settings
from app.db import dispose_engine
from app.observability.logging import configure_logging


async def main() -> None:
    bot = Bot(token=get_settings().bot_token.get_secret_value())
    # MemoryStorage: запомненный join_-код теряется при перезапуске бота (допустимо для pre-MVP)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(main())
