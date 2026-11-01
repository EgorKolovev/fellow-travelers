from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Contact,
    InlineKeyboardMarkup,
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


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID)
    )


def make_message(*, user_id: int = TG_ID, contact: Contact | None = None) -> MagicMock:
    message = MagicMock()
    message.from_user = TgUser(id=user_id, is_bot=False, first_name="Анна", username="anna")
    message.contact = contact
    message.answer = AsyncMock()
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
    assert markup.one_time_keyboard
    assert markup.resize_keyboard
    assert "телефон" in message.answer.call_args.args[0]
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
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_urls(markup) == [
        "https://t.me/test_bot?startapp",
        "https://t.me/test_bot?startapp=admin",
    ]
    assert inline_texts(markup) == [texts.OPEN_APP_BUTTON, texts.ADMIN_BUTTON]


async def test_start_with_phone_and_join_code_shows_join_button(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state)
    await share(state)
    message = await start(state, "join_ABC123")
    markup = last_markup(message)
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_texts(markup) == [texts.JOIN_BUTTON, texts.ADMIN_BUTTON]
    assert inline_urls(markup) == [
        "https://t.me/test_bot?startapp=join_ABC123",
        "https://t.me/test_bot?startapp=admin",
    ]


async def test_join_payload_survives_contact(db_session: AsyncSession, state: FSMContext) -> None:
    await start(state, "join_ABC123")
    answer = await share(state)
    assert answer.answer.await_count == 2
    first = answer.answer.call_args_list[0]
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
    markup = last_markup(answer)
    assert isinstance(markup, InlineKeyboardMarkup)
    assert inline_urls(markup)[0] == "https://t.me/test_bot?startapp=join_ABC123"
    assert inline_texts(markup)[0] == texts.JOIN_BUTTON
    user = await get_user(db_session)
    assert user is not None
    assert user.phone == "+79991234567"


async def test_plain_start_forgets_previous_invite(
    db_session: AsyncSession, state: FSMContext
) -> None:
    await start(state, "join_ABC123")
    await share(state)
    message = await start(state)
    assert inline_urls(last_markup(message))[0] == mini_app_link(None)
    assert inline_texts(last_markup(message))[0] == texts.OPEN_APP_BUTTON


@pytest.mark.parametrize("payload", ["join_AB", "join_ABC1234", "join_AB-123", "admin", "x"])
async def test_invalid_payload_ignored(
    db_session: AsyncSession, state: FSMContext, payload: str
) -> None:
    await start(state, payload)
    answer = await share(state)
    assert inline_urls(last_markup(answer))[0] == mini_app_link(None)
    assert inline_texts(last_markup(answer))[0] == texts.OPEN_APP_BUTTON


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


async def test_other_message_with_phone(db_session: AsyncSession, state: FSMContext) -> None:
    await share(state)
    message = make_message()
    await handlers.on_other(message, state)
    assert isinstance(last_markup(message), InlineKeyboardMarkup)


def test_mini_app_link() -> None:
    assert mini_app_link(None) == "https://t.me/test_bot?startapp"
    assert mini_app_link("join_ABC123") == "https://t.me/test_bot?startapp=join_ABC123"
