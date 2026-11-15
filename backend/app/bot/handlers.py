import logging
import re
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.types import User as TgUser

from app.bot import keyboards, texts
from app.config import get_settings
from app.db import get_session_factory
from app.services.users import InvalidPhoneError, TelegramProfile, save_phone, upsert_profile

router = Router(name="main")
router.message.filter(F.chat.type == "private")

_JOIN_PAYLOAD = re.compile(r"join_([A-Za-z0-9]{6})")
_JOIN_KEY = "join_code"


def _profile(tg: TgUser) -> TelegramProfile:
    return TelegramProfile(
        telegram_id=tg.id,
        first_name=tg.first_name,
        last_name=tg.last_name,
        username=tg.username,
    )


async def _answer_main(message: Message, state: FSMContext, *, has_phone: bool, text: str) -> None:
    """Просит контакт, если телефона нет, иначе показывает inline-кнопки приложения."""
    if has_phone:
        join_code: str | None = (await state.get_data()).get(_JOIN_KEY)
        await message.answer(text, reply_markup=keyboards.app_keyboard(join_code))
    else:
        await message.answer(texts.PHONE_REQUEST, reply_markup=keyboards.phone_keyboard())


def _ready_text(join_code: str | None) -> str:
    return texts.READY_NO_CODE if join_code is None else texts.READY_JOIN


async def _set_app_menu(message: Message, *, enabled: bool) -> None:
    """Включает или сбрасывает кнопку меню мини-приложения в этом чате; сбои игнорирует."""
    if message.bot is None:
        return
    menu_button: MenuButtonDefault | MenuButtonWebApp
    if enabled:
        web_app_url = get_settings().web_app_url
        if web_app_url is None:
            return
        menu_button = MenuButtonWebApp(
            text=texts.OPEN_APP_BUTTON, web_app=WebAppInfo(url=web_app_url)
        )
    else:
        menu_button = MenuButtonDefault()
    try:
        await message.bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=menu_button)
    except TelegramAPIError:
        logging.warning("Не удалось обновить кнопку меню", exc_info=True)


@router.message(Command("start"))
async def on_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    tg = message.from_user
    if tg is None:
        return
    # Код держим в FSM (MemoryStorage) до получения телефона. Каждый /start перезаписывает его:
    # без валидного payload старое приглашение не должно «прилипать» к кнопке.
    match = _JOIN_PAYLOAD.fullmatch(command.args) if command.args else None
    await state.update_data({_JOIN_KEY: match.group(1) if match else None})
    async with get_session_factory()() as session:
        user = await upsert_profile(session, _profile(tg))
    join_code = match.group(1) if match else None
    await _set_app_menu(message, enabled=user.phone is not None)
    await _answer_main(
        message, state, has_phone=user.phone is not None, text=_ready_text(join_code)
    )


@router.message(F.contact)
async def on_contact(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    contact = message.contact
    if tg is None or contact is None:
        return
    if contact.user_id != tg.id:
        await message.answer(texts.FOREIGN_CONTACT, reply_markup=keyboards.phone_keyboard())
        return
    try:
        async with get_session_factory()() as session:
            await save_phone(session, _profile(tg), contact.phone_number)
    except InvalidPhoneError:
        await message.answer(texts.INVALID_PHONE, reply_markup=keyboards.phone_keyboard())
        return
    # Reply-клавиатуру убираем служебным сообщением (в одном сообщении возможна только одна
    # разметка) и сразу его удаляем, чтобы в чате осталось одно сообщение «готово».
    tmp = await message.answer(texts.REMOVE_KEYBOARD, reply_markup=ReplyKeyboardRemove())
    with suppress(TelegramBadRequest):
        await tmp.delete()
    await _set_app_menu(message, enabled=True)
    join_code: str | None = (await state.get_data()).get(_JOIN_KEY)
    await _answer_main(message, state, has_phone=True, text=_ready_text(join_code))


@router.message()
async def on_other(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    if tg is None:
        return
    async with get_session_factory()() as session:
        user = await upsert_profile(session, _profile(tg))
    await _set_app_menu(message, enabled=user.phone is not None)
    await _answer_main(message, state, has_phone=user.phone is not None, text=texts.HINT)
