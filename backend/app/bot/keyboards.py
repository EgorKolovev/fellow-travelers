from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts
from app.config import get_settings


def mini_app_link(param: str | None) -> str:
    """Ссылка на Main Mini App бота; без параметра — `?startapp`."""
    base = f"https://t.me/{get_settings().bot_username}?startapp"
    return base if param is None else f"{base}={param}"


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.SHARE_PHONE_BUTTON, request_contact=True)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def app_keyboard(join_code: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки приложения; с кодом приглашения первая кнопка ведёт на вступление в группу."""
    if join_code is None:
        first = InlineKeyboardButton(text=texts.OPEN_APP_BUTTON, url=mini_app_link(None))
    else:
        first = InlineKeyboardButton(text=texts.JOIN_BUTTON, url=mini_app_link(f"join_{join_code}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [first],
            [InlineKeyboardButton(text=texts.ADMIN_BUTTON, url=mini_app_link("admin"))],
        ]
    )
