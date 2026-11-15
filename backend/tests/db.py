"""Фикстуры тестовой БД. Ленивые: тесты, которые их не запрашивают, не требуют PostgreSQL."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.seeds.locations import seed_locations

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_UNAVAILABLE_HINT = "Тестовая БД недоступна: запустите `docker compose up -d db` (см. README)."


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def database_url() -> str:
    # conftest уже выставил DATABASE_URL равным TEST_DATABASE_URL
    url = os.environ["DATABASE_URL"]
    # фикстуры делают downgrade base — защищаемся от случайного указания рабочей БД
    if not str(make_url(url).database).endswith("_test"):
        pytest.fail("TEST_DATABASE_URL должна указывать на БД с суффиксом _test", pytrace=False)
    return url


async def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    admin_engine = create_async_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
            )
            if not exists:
                # имя БД нельзя передать параметром; кавычки экранируем
                quoted = '"' + str(parsed.database).replace('"', '""') + '"'
                await conn.execute(text(f"CREATE DATABASE {quoted}"))
    finally:
        await admin_engine.dispose()


async def reseed_locations(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            await seed_locations(session)
    finally:
        await engine.dispose()


def migrate_from_scratch(url: str) -> None:
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
    asyncio.run(reseed_locations(url))


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Создаёт тестовую БД, прогоняет downgrade base + upgrade head и сидит справочник.

    Синхронная фикстура: env.py Alembic сам вызывает asyncio.run, а это возможно только пока
    в потоке не запущен цикл событий. Синхронные фикстуры выполняются вне цикла, поэтому
    отдельный поток не нужен.
    """
    url = database_url()
    try:
        asyncio.run(_create_database_if_missing(url))
        migrate_from_scratch(url)
    except (OSError, DBAPIError) as exc:
        pytest.fail(f"{DB_UNAVAILABLE_HINT} ({type(exc).__name__}: {exc})", pytrace=False)
    return url


@pytest.fixture(scope="session")
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    # как в app.db: параметры SQL не попадают в логи и тексты исключений
    engine = create_async_engine(migrated_database, hide_parameters=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Сессия во внешней транзакции, которая откатывается в конце теста.

    commit() кода приложения освобождает только savepoint, поэтому изоляция сохраняется.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
