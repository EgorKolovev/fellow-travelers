"""Гонки между соединениями: SKIP LOCKED у воркеров и блокировка группы в правке рейса.

`db_session` держит одну транзакцию на одном соединении и гонку не покажет, поэтому здесь
сессии открываются прямо на `db_engine` и данные коммитятся. Фикстура `group` удаляет их
после теста: каскад от группы уносит рейсы и задания.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    BackgroundJob,
    Event,
    Trip,
    User,
)
from app.schemas.trip import TripUpdate
from app.services.audit import Actor
from app.services.jobs import JobOutcome, enqueue_reconcile, process_next
from app.services.matching import lock_event, match_application
from app.services.trips import update_trip
from tests.api.conftest import auth_headers
from tests.factories import make_application, make_event, make_trip, make_user
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.concurrency import client_on_engine, wait, wait_for_lock_waiter
from tests.helpers.contract import assert_contract

ADMIN_TELEGRAM_ID = 7_000_001
MEMBER_TELEGRAM_IDS = (7_000_011, 7_000_012)
ESTIMATE = datetime(2026, 11, 12, 6, 30, tzinfo=UTC)
ARRIVAL = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)


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


async def _enqueue(sessions: async_sessionmaker[AsyncSession], group: Group) -> None:
    async with sessions() as session:
        await enqueue_reconcile(session, event_id=group.event_id, trip_id=group.trip_id)
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

        assert await wait(process_next(sessions)) is None  # не ждёт, а пропускает
        assert await _job_count(sessions, group) == 1
        await holder.rollback()

    assert await wait(process_next(sessions)) == JobOutcome.DONE
    assert await _job_count(sessions, group) == 0


async def test_two_workers_share_one_job(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _enqueue(sessions, group)

    outcomes = await wait(asyncio.gather(process_next(sessions), process_next(sessions)))

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
            await wait_for_lock_waiter(sessions, worker)
            assert await _job_count(sessions, group) == 1  # задание ещё не выполнено и не удалено
            await holder.rollback()
            assert await wait(worker) == JobOutcome.DONE
        finally:
            if not worker.done():
                worker.cancel()
    assert await _job_count(sessions, group) == 0


# ---------------------------------------------------------------- правка рейса и блокировка


@pytest.fixture
async def engine_client(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    async with client_on_engine(app, sessions) as client:
        yield client


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
            await wait_for_lock_waiter(sessions, request)
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
            response = await wait(request)
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
            await wait_for_lock_waiter(sessions, request)
            await rival.rollback()  # соперник ничего не менял
            response = await wait(request)
        finally:
            if not request.done():
                request.cancel()

    assert response.status_code == 200
    async with sessions() as session:
        (entry,) = await audit_rows(session, group.event_id)
        assert entry.action == "trip_updated"
    assert await _job_count(sessions, group) == 1


# ---------------------------------------------------------------- слияние заданий без дедлока


@dataclass(frozen=True)
class PairGroup:
    admin_id: uuid.UUID
    event_id: uuid.UUID
    trip_ids: tuple[uuid.UUID, uuid.UUID]
    application_ids: tuple[uuid.UUID, uuid.UUID]
    user_ids: tuple[uuid.UUID, uuid.UUID]


@pytest.fixture
async def pair_group(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[PairGroup]:
    """Закоммиченные админ, группа и пара в одном трансфере (рейсы в одно время)."""
    async with sessions() as session:
        admin = await make_user(
            session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan", phone="+79990007001"
        )
        event = await make_event(session, admin_id=admin.id)
        members = [
            await make_user(
                session, telegram_id=telegram_id, phone=f"+7999000{telegram_id % 10000}"
            )
            for telegram_id in MEMBER_TELEGRAM_IDS
        ]
        trips = [
            await make_trip(session, event_id=event.id, scheduled_arrival=ARRIVAL) for _ in members
        ]
        applications = [
            await make_application(
                session, event_id=event.id, user_id=user.id, trip_id=trip.id, baggage_count=0
            )
            for user, trip in zip(members, trips, strict=True)
        ]
        assert await match_application(session, applications[1]) is not None
        await session.commit()
        created = PairGroup(
            admin_id=admin.id,
            event_id=event.id,
            trip_ids=(trips[0].id, trips[1].id),
            application_ids=(applications[0].id, applications[1].id),
            user_ids=(members[0].id, members[1].id),
        )
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(
                delete(User).where(User.id.in_((created.admin_id, *created.user_ids)))
            )
            await session.commit()


async def _edit_arrival(
    session: AsyncSession, group: PairGroup, trip_id: uuid.UUID, estimated: datetime
) -> None:
    """Правка рейса админом, как `PATCH /trips/{id}`: блокировка группы, запись, задание, commit."""
    trip = await session.get(Trip, trip_id)
    assert trip is not None
    await update_trip(
        session,
        trip,
        TripUpdate(estimated_arrival=estimated),
        Actor(ActorKind.ADMIN, group.admin_id),
    )


async def test_edit_while_worker_waits_for_event_lock_merges_without_deadlock(
    sessions: async_sessionmaker[AsyncSession], pair_group: PairGroup
) -> None:
    first_trip, second_trip = pair_group.trip_ids
    # первая правка безобидна (в пределах ожидания) и оставляет задание
    async with sessions() as session:
        await wait(_edit_arrival(session, pair_group, first_trip, ARRIVAL + timedelta(minutes=5)))
    assert await _job_count_of(sessions, pair_group.event_id) == 1

    async with sessions() as producer:
        # вторая правка уже держит группу, когда воркер забирает задание и встаёт за блокировкой
        await lock_event(producer, pair_group.event_id)
        worker = asyncio.ensure_future(process_next(sessions))
        try:
            await wait_for_lock_waiter(sessions, worker)

            # правка делает пару несовместимой и ставит задание. Строка первого задания
            # заблокирована воркером, а воркер ждёт группу: DO UPDATE дал бы взаимную блокировку,
            # DO NOTHING не ждёт строку и сливает правку в уже стоящее задание
            await wait(
                _edit_arrival(producer, pair_group, second_trip, ARRIVAL + timedelta(minutes=40))
            )
            assert await wait(worker) == JobOutcome.DONE
        finally:
            if not worker.done():
                worker.cancel()

    async with sessions() as session:
        # второй правке отдельное задание не понадобилось: пересчёт видит обе правки
        assert await _job_count_of(sessions, pair_group.event_id) == 0
        applications = {
            row.id: row
            for row in (
                await session.scalars(
                    select(Application)
                    .where(Application.event_id == pair_group.event_id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
        assert {row.status for row in applications.values()} == {ApplicationStatus.SEARCHING}
        assert {row.transfer_id for row in applications.values()} == {None}
        rows = await audit_rows(session, pair_group.event_id)
        assert [row.action for row in rows].count(AuditAction.TRIP_UPDATED) == 2
        excluded = [row for row in rows if row.reason == "incompatible_after_change"]
        assert len(excluded) == 1
        assert excluded[0].trip_id == first_trip  # слитое задание помнит первую правку
        # кого именно исключить, решает tie-break по changed: задание помнит первый рейс, поэтому
        # ушла заявка на нём. Состав при этом корректен: пара несовместима и распущена
        assert excluded[0].application_id == pair_group.application_ids[0]
        await assert_history_matches_transfers(session, pair_group.event_id)


async def _job_count_of(sessions: async_sessionmaker[AsyncSession], event_id: uuid.UUID) -> int:
    async with sessions() as session:
        rows = await session.scalars(
            select(BackgroundJob.id).where(BackgroundJob.event_id == event_id)
        )
        return len(rows.all())
