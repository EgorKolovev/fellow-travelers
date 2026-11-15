"""Помощники для тестов гонок: сессии прямо на движке, ожидание блокировки, клиент на движке."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_session

TIMEOUT = 10.0


async def wait[T](awaitable: Awaitable[T]) -> T:
    """Ждёт не дольше TIMEOUT: взаимная блокировка должна давать падение, а не зависание."""
    return await asyncio.wait_for(awaitable, TIMEOUT)


async def wait_for_lock_waiter(
    sessions: async_sessionmaker[AsyncSession], blocked: asyncio.Future[Any]
) -> None:
    """Ждёт, пока какая-нибудь сессия встанет в очередь за advisory-блокировкой."""
    query = text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    for _ in range(200):
        assert not blocked.done(), "задача завершилась, не дождавшись блокировки группы"
        async with sessions() as session:
            if await session.scalar(query):
                return
        await asyncio.sleep(0.025)
    pytest.fail("никто не ждёт блокировку группы")


@asynccontextmanager
async def client_on_engine(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент, у которого каждый запрос идёт в своей сессии на движке, а не в `db_session`."""

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
