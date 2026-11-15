from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Contact,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import handlers, texts
from app.bot.keyboards import mini_app_link
from app.models import User

TG_ID = 777_001


@pytest.fixture(autouse=True)
def _use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def session_cm() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(handlers, "get_session_factory", lambda: session_cm)


WEB_APP_URL = "https://app.example.com"


@pytest.fixture(autouse=True)
def _web_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "get_settings", lambda: SimpleNamespace(web_app_url=WEB_APP_URL))


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID)
    )


def make_message(*, user_id: int = TG_ID, contact: Contact | None = None) -> MagicMock:
    message = MagicMock()
    message.from_user = TgUser(id=user_id, is_bot=False, first_name="Анна", username="anna")
    message.contact = contact
    sent = MagicMock()
    sent.delete = AsyncMock()
    message.sent = sent
    message.chat.id = user_id
    message.bot = MagicMock()
    message.bot.set_chat_menu_button = AsyncMock()
    message.answer = AsyncMock(return_value=sent)
    return message


def last_markup(message: MagicMock) -> Any:
    return message.answer.call_args.kwargs["reply_markup"]


def inline_urls(markup: InlineKeyboardMarkup) -> list[str | None]:
    return [b.url for row in markup.inline_keyboard for b in row]


def inline_texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def get_user(session: AsyncSession) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == TG_ID))
    return result.scalar_one_or_none()


async def start(state: FSMContext, args: str | None = None) -> MagicMock:
    message = make_message()
    await handlers.on_start(message, CommandObject(command="start", args=args), state)
    return message


async def share(
    state: FSMContext, *, user_id: int = TG_ID, phone: str = "79991234567"
) -> MagicMock:
    message = make_message(contact=Contact(phone_number=phone, first_name="Анна", user_id=user_id))
    await handlers.on_contact(message, state)
    return message


async def test_start_without_phone_requests_contact(
    db_session: AsyncSession, state: FSMContext
) -> None:
    message = await start(state)
    markup = last_markup(message)
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.keyboard[0][0].request_contact is True
    assert markup.keyboard[0][0].text == "Поделиться контактом"
    assert markup.keyboard[0][0].style == "primary"
    assert markup.resize_keyboard
    assert markup.is_persistent
    assert not markup.one_time_keyboard
    assert markup.input_field_placeholder == "Нажмите «Поделиться контактом»"
    assert message.answer.await_count == 1
    assert message.answer.call_args.args[0] == (
        "Чтобы начать, поделитесь контактом — он понадобится для связи"
    )
    user = await get_user(db_session)
    assert user is not None
    assert user.first_name == "Анна"
    assert user.phone is None


async def test_start_with_phone_shows_inline_buttons(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state)
    await share(state)
    message = await start(state)
    markup = last_markup(message)
    assert message.answer.call_args.args[0] == texts.READY_NO_CODE
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_urls(markup) == ["https://t.me/test_bot?startapp=admin"]
    assert inline_texts(markup) == [texts.ADMIN_BUTTON]


async def test_start_with_phone_and_join_code_shows_join_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state)
    await share(state)
    message = await start(state, "join_ABC123")
    markup = last_markup(message)
    assert message.answer.call_args.args[0] == (
        "Готово, теперь можно перейти к данным о рейсе, чтобы найти попутчиков"
    )
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_texts(markup) == [texts.JOIN_BUTTON]
    assert inline_urls(markup) == ["https://t.me/test_bot?startapp=join_ABC123"]


async def test_join_payload_survives_contact(db_session: AsyncSession, state: FSMContext) -> None:
    await start(state, "join_ABC123")
    answer = await share(state)
    assert answer.answer.await_count == 2
    first = answer.answer.call_args_list[0]
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
    answer.sent.delete.assert_awaited_once()
    assert answer.answer.call_args.args[0] == texts.READY_JOIN
    markup = last_markup(answer)
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_urls(markup)[0] == "https://t.me/test_bot?startapp=join_ABC123"
    assert inline_texts(markup)[0] == "Заполнить данные о рейсе →"
    user = await get_user(db_session)
    assert user is not None
    assert user.phone == "+79991234567"


async def test_contact_survives_delete_failure(db_session: AsyncSession, state: FSMContext) -> None:
    await start(state)
    message = make_message(
        contact=Contact(phone_number="79991234567", first_name="А", user_id=TG_ID)
    )
    message.sent.delete = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message can't be deleted")
    )
    await handlers.on_contact(message, state)
    assert message.answer.await_count == 2
    assert isinstance(last_markup(message), InlineKeyboardMarkup)


async def test_plain_start_forgets_previous_invite(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state, "join_ABC123")
    await share(state)
    message = await start(state)
    assert inline_urls(last_markup(message)) == [mini_app_link("admin")]
    assert inline_texts(last_markup(message)) == [texts.ADMIN_BUTTON]


@pytest.mark.parametrize("payload", ["join_AB", "join_ABC1234", "join_AB-123", "admin", "x"])
async def test_invalid_payload_ignored(
    db_session: AsyncSession, state: FSMContext, payload: str
) -> None:
    await start(state, payload)
    answer = await share(state)
    assert inline_urls(last_markup(answer)) == [mini_app_link("admin")]
    assert inline_texts(last_markup(answer)) == [texts.ADMIN_BUTTON]


async def test_foreign_contact_rejected(db_session: AsyncSession, state: FSMContext) -> None:
    await start(state)
    answer = await share(state, user_id=TG_ID + 1)
    assert "свой контакт" in answer.answer.call_args.args[0]
    assert isinstance(last_markup(answer), ReplyKeyboardMarkup)
    user = await get_user(db_session)
    assert user is not None
    assert user.phone is None


async def test_invalid_phone_rejected(db_session: AsyncSession, state: FSMContext) -> None:
    await start(state)
    answer = await share(state, phone="12")
    assert isinstance(last_markup(answer), ReplyKeyboardMarkup)
    user = await get_user(db_session)
    assert user is not None
    assert user.phone is None


async def test_contact_creates_missing_user(db_session: AsyncSession, state: FSMContext) -> None:
    await share(state)
    user = await get_user(db_session)
    assert user is not None
    assert user.phone == "+79991234567"


async def test_other_message_without_phone(db_session: AsyncSession, state: FSMContext) -> None:
    message = make_message()
    await handlers.on_other(message, state)
    assert isinstance(last_markup(message), ReplyKeyboardMarkup)
    assert message.answer.call_args.args[0] == texts.PHONE_REQUEST


async def test_other_message_with_phone(db_session: AsyncSession, state: FSMContext) -> None:
    await share(state)
    message = make_message()
    await handlers.on_other(message, state)
    assert isinstance(last_markup(message), InlineKeyboardMarkup)
    assert message.answer.call_args.args[0] == texts.HINT


def test_mini_app_link() -> None:
    assert mini_app_link(None) == "https://t.me/test_bot?startapp"
    assert mini_app_link("join_ABC123") == "https://t.me/test_bot?startapp=join_ABC123"


async def test_contact_enables_menu_button(db_session: AsyncSession, state: FSMContext) -> None:
    message = await share(state)
    message.bot.set_chat_menu_button.assert_awaited_once()
    kwargs = message.bot.set_chat_menu_button.call_args.kwargs
    assert kwargs["chat_id"] == TG_ID
    button = kwargs["menu_button"]
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == texts.OPEN_APP_BUTTON
    assert button.web_app.url == WEB_APP_URL


async def test_start_with_phone_enables_menu_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state)
    await share(state)
    message = await start(state)
    message.bot.set_chat_menu_button.assert_awaited_once()
    assert message.bot.set_chat_menu_button.call_args.kwargs["chat_id"] == TG_ID


async def test_start_without_phone_resets_menu_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    message = await start(state)
    message.bot.set_chat_menu_button.assert_awaited_once()
    kwargs = message.bot.set_chat_menu_button.call_args.kwargs
    assert kwargs["chat_id"] == TG_ID
    assert isinstance(kwargs["menu_button"], MenuButtonDefault)


async def test_no_menu_button_without_web_app_url(
    db_session: AsyncSession, state: FSMContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "get_settings", lambda: SimpleNamespace(web_app_url=None))
    message = await share(state)
    message.bot.set_chat_menu_button.assert_not_awaited()
    assert message.answer.await_count == 2


async def test_menu_button_failure_does_not_break_onboarding(
    db_session: AsyncSession, state: FSMContext
) -> None:
    message = make_message(
        contact=Contact(phone_number="79991234567", first_name="А", user_id=TG_ID)
    )
    message.bot.set_chat_menu_button = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="bad request")
    )
    await handlers.on_contact(message, state)
    assert message.answer.await_count == 2
    assert message.answer.call_args.args[0] == texts.READY_NO_CODE
    assert isinstance(last_markup(message), InlineKeyboardMarkup)


async def test_other_without_phone_resets_menu_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    message = make_message()
    await handlers.on_other(message, state)
    kwargs = message.bot.set_chat_menu_button.call_args.kwargs
    assert kwargs["chat_id"] == TG_ID
    assert isinstance(kwargs["menu_button"], MenuButtonDefault)


async def test_other_with_phone_enables_menu_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await share(state)
    message = make_message()
    await handlers.on_other(message, state)
    kwargs = message.bot.set_chat_menu_button.call_args.kwargs
    assert kwargs["chat_id"] == TG_ID
    assert isinstance(kwargs["menu_button"], MenuButtonWebApp)
