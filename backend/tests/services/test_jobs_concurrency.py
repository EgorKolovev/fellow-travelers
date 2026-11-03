"""Гонки между соединениями: SKIP LOCKED у воркеров и блокировка группы в правке рейса.

`db_session` держит одну транзакцию на одном соединении и гонку не покажет, поэтому здесь
сессии открываются прямо на `db_engine` и данные коммитятся. Фикстура `group` удаляет их
после теста: каскад от группы уносит рейсы и задания.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import get_session
from app.models import BackgroundJob, Event, Trip, User
from app.services.jobs import JobOutcome, enqueue_reconcile, process_next
from app.services.matching import lock_event
from tests.api.conftest import auth_headers
from tests.factories import make_event, make_trip, make_user
from tests.helpers.audit_replay import audit_rows
from tests.helpers.contract import assert_contract

ADMIN_TELEGRAM_ID = 7_000_001
TIMEOUT = 10.0
ESTIMATE = datetime(2026, 11, 12, 6, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Group:
    admin_id: uuid.UUID
    event_id: uuid.UUID
    trip_id: uuid.UUID


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def group(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Group]:
    """Закоммиченные админ, группа и рейс; после теста всё удаляется."""
    async with sessions() as session:
        admin = await make_user(
            session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan", phone="+79990007001"
        )
        event = await make_event(session, admin_id=admin.id)
        trip = await make_trip(session, event_id=event.id)
        await session.commit()
        created = Group(admin_id=admin.id, event_id=event.id, trip_id=trip.id)
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(delete(User).where(User.id == created.admin_id))
            await session.commit()


async def _wait[T](awaitable: Awaitable[T]) -> T:
    """Ждёт не дольше TIMEOUT: взаимная блокировка должна давать падение, а не зависание."""
    return await asyncio.wait_for(awaitable, TIMEOUT)


async def _wait_for_lock_waiter(
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


async def _enqueue(sessions: async_sessionmaker[AsyncSession], group: Group) -> None:
    async with sessions() as session:
        enqueue_reconcile(session, event_id=group.event_id, trip_id=group.trip_id)
        await session.commit()


async def _job_count(sessions: async_sessionmaker[AsyncSession], group: Group) -> int:
    async with sessions() as session:
        rows = await session.scalars(
            select(BackgroundJob.id).where(BackgroundJob.event_id == group.event_id)
        )
        return len(rows.all())


# ---------------------------------------------------------------- SKIP LOCKED


async def test_locked_job_is_skipped_by_other_worker(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _enqueue(sessions, group)

    async with sessions() as holder:
        locked = await holder.scalars(
            select(BackgroundJob).where(BackgroundJob.event_id == group.event_id).with_for_update()
        )
        assert len(locked.all()) == 1

        assert await _wait(process_next(sessions)) is None  # не ждёт, а пропускает
        assert await _job_count(sessions, group) == 1
        await holder.rollback()

    assert await _wait(process_next(sessions)) == JobOutcome.DONE
    assert await _job_count(sessions, group) == 0


async def test_two_workers_share_one_job(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _enqueue(sessions, group)

    outcomes = await _wait(asyncio.gather(process_next(sessions), process_next(sessions)))

    assert sorted(outcomes, key=str) == [None, JobOutcome.DONE]
    assert await _job_count(sessions, group) == 0


async def test_worker_waits_for_event_lock_and_keeps_job_meanwhile(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _enqueue(sessions, group)
    async with sessions() as holder:
        await lock_event(holder, group.event_id)
        worker = asyncio.ensure_future(process_next(sessions))
        try:
            await _wait_for_lock_waiter(sessions, worker)
            assert await _job_count(sessions, group) == 1  # задание ещё не выполнено и не удалено
            await holder.rollback()
            assert await _wait(worker) == JobOutcome.DONE
        finally:
            if not worker.done():
                worker.cancel()
    assert await _job_count(sessions, group) == 0


# ---------------------------------------------------------------- правка рейса и блокировка


@pytest.fixture
async def engine_client(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент, у которого каждый запрос идёт в своей сессии на движке, а не в `db_session`."""

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
        yield client
    app.dependency_overrides.clear()


async def test_patch_waits_for_event_lock_then_rereads_trip(
    engine_client: AsyncClient, sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    payload = {"estimated_arrival": ESTIMATE.isoformat()}
    headers = auth_headers(ADMIN_TELEGRAM_ID)
    async with sessions() as rival:
        await lock_event(rival, group.event_id)
        request = asyncio.ensure_future(
            engine_client.patch(f"/trips/{group.trip_id}", json=payload, headers=headers)
        )
        try:
            await _wait_for_lock_waiter(sessions, request)
            # пока группа занята, правка рейса не выполняется
            async with sessions() as reader:
                waiting = await reader.scalar(
                    select(Trip.estimated_arrival).where(Trip.id == group.trip_id)
                )
            assert waiting is None
            assert not request.done()
            # соперник успевает первым: то же расчётное прибытие, и он отпускает группу
            await rival.execute(
                update(Trip).where(Trip.id == group.trip_id).values(estimated_arrival=ESTIMATE)
            )
            await rival.commit()
            response = await _wait(request)
        finally:
            if not request.done():
                request.cancel()

    assert_contract(response, "PATCH", "/trips/{trip_id}")
    assert response.status_code == 200
    # рейс перечитан после блокировки: для правки он уже имеет это время, менять нечего
    async with sessions() as session:
        assert await audit_rows(session, group.event_id) == []
    assert await _job_count(sessions, group) == 0


async def test_patch_after_lock_release_enqueues_job_for_real_change(
    engine_client: AsyncClient, sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    payload = {"estimated_arrival": ESTIMATE.isoformat()}
    async with sessions() as rival:
        await lock_event(rival, group.event_id)
        request = asyncio.ensure_future(
            engine_client.patch(
                f"/trips/{group.trip_id}", json=payload, headers=auth_headers(ADMIN_TELEGRAM_ID)
            )
        )
        try:
            await _wait_for_lock_waiter(sessions, request)
            await rival.rollback()  # соперник ничего не менял
            response = await _wait(request)
        finally:
            if not request.done():
                request.cancel()

    assert response.status_code == 200
    async with sessions() as session:
        (entry,) = await audit_rows(session, group.event_id)
        assert entry.action == "trip_updated"
    assert await _job_count(sessions, group) == 1
