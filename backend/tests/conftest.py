import os
from collections.abc import AsyncIterator

from tests.helpers.telegram import BOT_TOKEN

# get_settings() приложения и Alembic смотрят на тестовую БД
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://fellow:fellow@localhost:5434/fellow_travelers_test"
)
os.environ["BOT_TOKEN"] = BOT_TOKEN
os.environ["BOT_USERNAME"] = "test_bot"

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app

pytest_plugins = ["tests.db", "tests.logs"]


@pytest.fixture
def app() -> FastAPI:
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
