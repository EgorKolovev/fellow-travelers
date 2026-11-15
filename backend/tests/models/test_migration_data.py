"""Миграции 0003 и 0004 на данных, которые уже есть в БД до деплоя.

БД откатывается на 0002, в неё кладутся группы, трансферы и заявки «как на main», затем
`upgrade head`. Проверяется, что трансферы получили базовую историю (иначе состав, восстановленный
по ней, неверен после первой же посадки или выхода) и что действующие группы получили задание на
первый пересчёт, а прошедшие нет. Тест возвращает БД к чистому head в конце.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    Application,
    ApplicationStatus,
    BackgroundJob,
    JobKind,
    JobStatus,
    Transfer,
    TransferOrigin,
    TransferStatus,
    User,
)
from app.services.jobs import JobOutcome, process_next
from tests.api.conftest import auth_headers
from tests.db import alembic_config, migrate_from_scratch
from tests.factories import (
    make_application,
    make_event,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.concurrency import client_on_engine, wait
from tests.helpers.contract import assert_contract

ARRIVAL = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
TODAY = date.today()


@dataclass
class OldGroup:
    """Что лежало в БД на 0002: id группы и составы трансферов."""

    event_id: uuid.UUID
    transfers: dict[str, uuid.UUID] = field(default_factory=dict)
    members: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    waiting: list[uuid.UUID] = field(default_factory=list)


async def _to_0002(engine: AsyncEngine) -> None:
    # Alembic (env.py) сам вызывает asyncio.run, поэтому из работающего цикла — только в потоке
    await asyncio.to_thread(command.downgrade, alembic_config(), "0002")
    await engine.dispose()  # соединения пула знают про старую схему


async def _to_head(engine: AsyncEngine) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")
    await engine.dispose()


async def _add_member(
    session: AsyncSession, event_id: uuid.UUID, transfer_id: uuid.UUID | None = None, **kw: object
) -> Application:
    trip = await make_trip(session, event_id=event_id, scheduled_arrival=ARRIVAL)
    kw.setdefault("baggage_count", 0)
    application = await make_application(
        session, event_id=event_id, trip_id=trip.id, transfer_id=transfer_id, **kw
    )
    await make_membership(session, event_id=event_id, user_id=application.user_id)
    return application


async def _seed_group(
    session: AsyncSession, *, ends_on: date, with_waiting: bool = False
) -> OldGroup:
    """Группа как на main: пара, тройка, уехавший и ручной трансферы, при желании ждущая заявка."""
    event = await make_event(session, starts_on=ends_on - timedelta(days=2), ends_on=ends_on)
    group = OldGroup(event_id=event.id)
    layout = {
        "pair": (2, {}),
        "triple": (3, {}),
        "departed": (2, {"departed_at": ARRIVAL}),
        "manual": (2, {}),
    }
    for name, (size, extra) in layout.items():
        transfer = await make_transfer(
            session,
            event_id=event.id,
            origin=TransferOrigin.ADMIN if name == "manual" else TransferOrigin.AUTO,
            status=TransferStatus.DEPARTED if name == "departed" else TransferStatus.ACTIVE,
        )
        group.transfers[name] = transfer.id
        group.members[name] = [
            (await _add_member(session, event.id, transfer.id, **extra)).id for _ in range(size)
        ]
    if with_waiting:
        group.waiting.append((await _add_member(session, event.id)).id)
    return group


@pytest.fixture
async def at_0002(db_engine: AsyncEngine, migrated_database: str) -> AsyncIterator[AsyncEngine]:
    await _to_0002(db_engine)
    try:
        yield db_engine
    finally:
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


# ---------------------------------------------------------------- 0003: базовая история


async def test_existing_transfers_get_a_baseline_history(at_0002: AsyncEngine) -> None:
    async with AsyncSession(at_0002, expire_on_commit=False) as session:
        group = await _seed_group(session, ends_on=TODAY + timedelta(days=30))
        await session.commit()

    await _to_head(at_0002)

    async with AsyncSession(at_0002) as session:
        rows = await audit_rows(session, group.event_id)
        assert len(rows) == 4  # по одной записи на трансфер, любого статуса и происхождения
        by_transfer = {row.transfer_id: row for row in rows}
        assert set(by_transfer) == set(group.transfers.values())
        for name, transfer_id in group.transfers.items():
            row = by_transfer[transfer_id]
            transfer = await session.get(Transfer, transfer_id)
            assert transfer is not None
            assert (row.action, row.reason, row.actor_kind) == ("transfer_created", None, "system")
            assert (row.actor_user_id, row.subject_user_id, row.application_id) == (None,) * 3
            assert row.occurred_at == transfer.created_at
            expected = (
                await session.scalars(
                    select(Application.id)
                    .where(Application.transfer_id == transfer_id)
                    .order_by(Application.created_at, Application.id)
                )
            ).all()
            assert row.details == {"application_ids": [str(item) for item in expected]}, name
            assert set(expected) == set(group.members[name])
        await assert_history_matches_transfers(session, group.event_id)


async def test_transfer_without_members_gets_an_empty_baseline(at_0002: AsyncEngine) -> None:
    async with AsyncSession(at_0002, expire_on_commit=False) as session:
        event = await make_event(session)
        transfer = await make_transfer(session, event_id=event.id)
        await session.commit()

    await _to_head(at_0002)

    async with AsyncSession(at_0002) as session:
        (row,) = await audit_rows(session, event.id)
        assert (row.transfer_id, row.details) == (transfer.id, {"application_ids": []})
        await assert_history_matches_transfers(session, event.id)


async def test_history_stays_correct_after_join_and_leave_through_api(
    app: FastAPI, at_0002: AsyncEngine
) -> None:
    async with AsyncSession(at_0002, expire_on_commit=False) as session:
        group = await _seed_group(session, ends_on=TODAY + timedelta(days=30))
        newcomer = await make_user(session, telegram_id=7_100_001, phone="+79991000001")
        await make_membership(session, event_id=group.event_id, user_id=newcomer.id)
        leaver_application = await session.get(Application, group.members["triple"][0])
        assert leaver_application is not None
        leaver = await session.get(User, leaver_application.user_id)
        assert leaver is not None
        leaver_telegram_id = leaver.telegram_id
        trip = await make_trip(session, event_id=group.event_id, scheduled_arrival=ARRIVAL)
        await session.commit()
        trip_id = trip.id
    await _to_head(at_0002)
    sessions = async_sessionmaker(at_0002, expire_on_commit=False)

    async with client_on_engine(app, sessions) as client:
        # новая заявка садится к паре, собранной до деплоя
        joined = await wait(_put_application(client, group.event_id, 7_100_001, trip_id))
        pair_transfer = str(group.transfers["pair"])
        assert (joined.json()["status"], joined.json()["transfer_id"]) == (
            "assigned",
            pair_transfer,
        )
        async with sessions() as session:
            await assert_history_matches_transfers(session, group.event_id)
        # участник старой тройки выходит: двое остаются вместе
        left = await wait(
            client.post(
                f"/events/{group.event_id}/my-application/go-solo",
                headers=auth_headers(leaver_telegram_id),
            )
        )
        assert_contract(left, "POST", "/events/{event_id}/my-application/go-solo")
        assert left.json()["status"] == "solo"

    async with sessions() as session:
        actions = [(row.action, row.reason) for row in await audit_rows(session, group.event_id)]
        assert ("transfer_joined", "auto_match") in actions
        assert ("transfer_left", "go_solo") in actions
        await assert_history_matches_transfers(session, group.event_id)


async def _put_application(
    client: AsyncClient, event_id: uuid.UUID, telegram_id: int, trip_id: uuid.UUID
) -> Response:
    response = await client.put(
        f"/events/{event_id}/my-application",
        json={
            "trip_id": str(trip_id),
            "with_companion": False,
            "baggage_count": 0,
            "max_wait_minutes": 15,
        },
        headers=auth_headers(telegram_id),
    )
    assert_contract(response, "PUT", "/events/{event_id}/my-application")
    assert response.status_code == 201
    return response


# ---------------------------------------------------------------- 0004: первый пересчёт


async def test_current_groups_get_one_reconcile_job_and_past_groups_do_not(
    at_0002: AsyncEngine,
) -> None:
    async with AsyncSession(at_0002, expire_on_commit=False) as session:
        current = await _seed_group(session, ends_on=TODAY + timedelta(days=30), with_waiting=True)
        tomorrow = await _seed_group(session, ends_on=TODAY + timedelta(days=1))
        past = await _seed_group(session, ends_on=TODAY - timedelta(days=10), with_waiting=True)
        await session.commit()

    await _to_head(at_0002)

    async with AsyncSession(at_0002) as session:
        jobs = (await session.scalars(select(BackgroundJob))).all()
        assert {job.event_id for job in jobs} == {current.event_id, tomorrow.event_id}
        assert len(jobs) == 2
        for job in jobs:
            assert (job.kind, job.trip_id, job.status, job.attempts, job.last_error) == (
                JobKind.RECONCILE_TRIP,
                None,
                JobStatus.PENDING,
                0,
                None,
            )
        assert not any(job.event_id == past.event_id for job in jobs)
        # у прошедшей группы только базовая история: переписывать её незачем
        past_rows = await audit_rows(session, past.event_id)
        assert {row.action for row in past_rows} == {"transfer_created"}


async def test_first_reconcile_seats_waiting_application_in_old_pair(
    at_0002: AsyncEngine,
) -> None:
    async with AsyncSession(at_0002, expire_on_commit=False) as session:
        group = await _seed_group(session, ends_on=TODAY + timedelta(days=30), with_waiting=True)
        past = await _seed_group(session, ends_on=TODAY - timedelta(days=10), with_waiting=True)
        await session.commit()
    await _to_head(at_0002)
    sessions = async_sessionmaker(at_0002, expire_on_commit=False)
    (waiting_id,) = group.waiting
    (past_waiting_id,) = past.waiting

    outcomes = []
    while (outcome := await wait(process_next(sessions))) is not None:
        outcomes.append(outcome)

    assert outcomes == [JobOutcome.DONE]
    async with sessions() as session:
        waiting = await session.get(Application, waiting_id)
        assert waiting is not None
        # ждавшая заявка села в одну из машин, где ещё есть место (пара — единственная такая)
        assert waiting.status == ApplicationStatus.ASSIGNED
        assert waiting.transfer_id == group.transfers["pair"]
        past_waiting = await session.get(Application, past_waiting_id)
        assert past_waiting is not None
        assert (past_waiting.status, past_waiting.transfer_id) == (
            ApplicationStatus.SEARCHING,
            None,
        )
        rows = await audit_rows(session, group.event_id)
        joined = [row for row in rows if row.action == "transfer_joined"]
        assert [(row.reason, row.actor_kind, row.application_id) for row in joined] == [
            ("auto_fill", "system", waiting_id)
        ]
        assert joined[0].job_id is not None
        await assert_history_matches_transfers(session, group.event_id)
        past_rows = await audit_rows(session, past.event_id)
        assert {row.action for row in past_rows} == {"transfer_created"}
