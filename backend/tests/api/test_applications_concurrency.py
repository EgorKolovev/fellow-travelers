"""Одновременные `PUT /my-application` на отдельных сессиях и соединениях.

Заявки подают разные пользователи (или один и тот же дважды) в один момент. Сценарии сериализует
advisory-блокировка группы, поэтому гонка не должна дать ни заявку в двух трансферах, ни машину
сверх вместимости, ни 500. Данные коммитятся и удаляются после теста.
"""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import Application, ApplicationStatus, Event, Transfer, User
from tests.api.conftest import auth_headers
from tests.factories import make_event, make_membership, make_trip, make_user
from tests.helpers.audit_replay import assert_history_matches_transfers
from tests.helpers.concurrency import client_on_engine, wait
from tests.helpers.contract import assert_contract

FIRST_TELEGRAM_ID = 7_200_001
ARRIVAL = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
PATH = "/events/{event_id}/my-application"


@dataclass(frozen=True)
class Crowd:
    event_id: uuid.UUID
    user_ids: tuple[uuid.UUID, ...]
    telegram_ids: tuple[int, ...]
    trip_ids: tuple[uuid.UUID, ...]


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def client(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    async with client_on_engine(app, sessions) as engine_client:
        yield engine_client


@pytest.fixture
async def crowd(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Crowd]:
    """Закоммиченные группа и три её участника с телефонами, у каждого свой рейс в одно время."""
    async with sessions() as session:
        admin = await make_user(session)
        event = await make_event(session, admin_id=admin.id)
        users = [
            await make_user(
                session, telegram_id=FIRST_TELEGRAM_ID + index, phone=f"+7999720{index:04d}"
            )
            for index in range(3)
        ]
        trips = [
            await make_trip(session, event_id=event.id, scheduled_arrival=ARRIVAL) for _ in users
        ]
        for user in users:
            await make_membership(session, event_id=event.id, user_id=user.id)
        await session.commit()
        created = Crowd(
            event_id=event.id,
            user_ids=tuple(user.id for user in users),
            telegram_ids=tuple(user.telegram_id for user in users),
            trip_ids=tuple(trip.id for trip in trips),
        )
        admin_id = admin.id
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(delete(User).where(User.id.in_((admin_id, *created.user_ids))))
            await session.commit()


async def put_application(
    client: AsyncClient,
    crowd: Crowd,
    index: int,
    *,
    with_companion: bool = False,
    baggage_count: int = 0,
) -> Response:
    response = await client.put(
        f"/events/{crowd.event_id}/my-application",
        json={
            "trip_id": str(crowd.trip_ids[index]),
            "with_companion": with_companion,
            "baggage_count": baggage_count,
            "max_wait_minutes": 15,
        },
        headers=auth_headers(crowd.telegram_ids[index]),
    )
    assert_contract(response, "PUT", PATH)
    return response


async def assert_consistent(
    sessions: async_sessionmaker[AsyncSession], event_id: uuid.UUID
) -> dict[uuid.UUID, list[Application]]:
    """Заявка в одном трансфере, в машине не больше 3 пассажиров и 2 багажа, история сходится."""
    async with sessions() as session:
        applications = (
            await session.scalars(
                select(Application)
                .where(Application.event_id == event_id)
                .execution_options(populate_existing=True)
            )
        ).all()
        cars: dict[uuid.UUID, list[Application]] = defaultdict(list)
        for application in applications:
            assert (application.status == ApplicationStatus.ASSIGNED) == (
                application.transfer_id is not None
            )
            if application.transfer_id is not None:
                cars[application.transfer_id].append(application)
        transfer_ids = set(
            (await session.scalars(select(Transfer.id).where(Transfer.event_id == event_id))).all()
        )
        assert transfer_ids == set(cars), "трансфер без заявок или заявка в чужом трансфере"
        for members in cars.values():
            assert len(members) >= 2
            assert sum(2 if a.with_companion else 1 for a in members) <= 3
            assert sum(a.baggage_count for a in members) <= 2
        await assert_history_matches_transfers(session, event_id)
        return dict(cars)


async def test_three_concurrent_puts_end_in_one_consistent_transfer(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    responses = await wait(asyncio.gather(*(put_application(client, crowd, i) for i in range(3))))

    assert [response.status_code for response in responses] == [201, 201, 201]
    cars = await assert_consistent(sessions, crowd.event_id)
    # три совместимые заявки в любом порядке сходятся в одну машину
    assert [len(members) for members in cars.values()] == [3]


async def test_concurrent_puts_never_overfill_a_car(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    # 2 + 2 + 1 пассажиров не помещаются в одну машину
    responses = await wait(
        asyncio.gather(
            put_application(client, crowd, 0, with_companion=True),
            put_application(client, crowd, 1, with_companion=True),
            put_application(client, crowd, 2),
        )
    )

    assert [response.status_code for response in responses] == [201, 201, 201]
    cars = await assert_consistent(sessions, crowd.event_id)
    assert len(cars) <= 1  # допустима одна машина из 2 + 1; две заявки с попутчиками не вместе
    for members in cars.values():
        assert sum(1 for a in members if a.with_companion) <= 1


async def test_concurrent_puts_respect_baggage_limit(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    responses = await wait(
        asyncio.gather(*(put_application(client, crowd, i, baggage_count=2) for i in range(3)))
    )

    assert [response.status_code for response in responses] == [201, 201, 201]
    # у каждой по 2 места багажа: ни одна пара не помещается
    assert await assert_consistent(sessions, crowd.event_id) == {}


async def test_two_first_puts_of_one_user_make_one_application(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    responses = await wait(
        asyncio.gather(put_application(client, crowd, 0), put_application(client, crowd, 0))
    )

    assert sorted(response.status_code for response in responses) == [200, 201]
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Application)
            .where(Application.event_id == crowd.event_id, Application.user_id == crowd.user_ids[0])
        )
    assert count == 1
    await assert_consistent(sessions, crowd.event_id)
