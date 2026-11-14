import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonDefault

from app.bot.handlers import router
from app.bot.session import RelaySession
from app.config import get_settings
from app.db import dispose_engine


async def main() -> None:
    settings = get_settings()
    session = (
        RelaySession(
            str(settings.telegram_relay_url), settings.telegram_relay_secret.get_secret_value()
        )
        if settings.telegram_relay_url
        else None
    )
    bot = Bot(token=settings.bot_token.get_secret_value(), session=session)
    # MemoryStorage: запомненный join_-код теряется при перезапуске бота (допустимо для pre-MVP)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    try:
        # Глобально сбрасываем кнопку меню из @BotFather: она появится только после контакта
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except TelegramAPIError:
        logging.warning("Не удалось сбросить кнопку меню бота", exc_info=True)
    try:
        await dispatcher.start_polling(bot, polling_timeout=30)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
