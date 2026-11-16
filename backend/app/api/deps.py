from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.telegram import CurrentInitData
from app.config import get_settings
from app.db import get_session
from app.flights import FlightProvider, build_provider
from app.models import User
from app.services.users import TelegramProfile, upsert_profile

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(init_data: CurrentInitData, session: SessionDep) -> User:
    """Находит или создаёт пользователя по initData и обновляет его профиль из Telegram.

    Телефон не трогаем: его сохраняет бот.
    """
    tg = init_data.user
    return await upsert_profile(
        session,
        TelegramProfile(
            telegram_id=tg.id,
            first_name=tg.first_name,
            last_name=tg.last_name,
            username=tg.username,
            photo_url=tg.photo_url,
        ),
    )


CurrentUser = Annotated[User, Depends(get_current_user)]


@lru_cache
def _cached_provider() -> FlightProvider | None:
    return build_provider(get_settings())


def get_flight_provider() -> FlightProvider | None:
    """Поставщик рейсов из настроек (один на процесс); тесты подменяют его через overrides."""
    return _cached_provider()
