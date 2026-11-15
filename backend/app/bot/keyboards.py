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
        keyboard=[
            [KeyboardButton(text=texts.SHARE_PHONE_BUTTON, request_contact=True, style="primary")]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=texts.PHONE_INPUT_PLACEHOLDER,
    )


def app_keyboard(join_code: str | None = None) -> InlineKeyboardMarkup:
    """Одна кнопка: с кодом приглашения — вступление в группу, иначе — вход в админку."""
    if join_code is None:
        button = InlineKeyboardButton(text=texts.ADMIN_BUTTON, url=mini_app_link("admin"))
    else:
        button = InlineKeyboardButton(
            text=texts.JOIN_BUTTON, url=mini_app_link(f"join_{join_code}")
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
