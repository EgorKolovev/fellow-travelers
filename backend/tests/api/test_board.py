import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import ApplicationStatus, Event, TripStatus, User
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.factories import make_application, make_event, make_membership, make_trip, make_user
from tests.helpers.contract import assert_contract

MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"
BOARD_PATH = "/events/{event_id}/admin/board"
ADMIN_TELEGRAM_ID = 500
BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
COUNTER_KEYS = ["expected", "searching", "assigned", "solo", "at_meeting_point", "departed"]


async def get_board(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    telegram_id: int | None = ADMIN_TELEGRAM_ID,
    params: Any = None,
) -> Response:
    headers = {} if telegram_id is None else auth_headers(telegram_id)
    response = await client.get(f"/events/{event_id}/admin/board", params=params, headers=headers)
    assert_contract(response, "GET", BOARD_PATH)
    return response


def counters(data: dict[str, int]) -> list[int]:
    assert list(data) == COUNTER_KEYS
    return [data[k] for k in COUNTER_KEYS]


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await make_user(db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan")


@pytest.fixture
async def event(db_session: AsyncSession, admin: User) -> Event:
    return await make_event(db_session, admin_id=admin.id)


async def trip(session: AsyncSession, event: Event, minutes: int = 0, **kw: Any) -> Any:
    return await make_trip(
        session, event_id=event.id, scheduled_arrival=BASE + timedelta(minutes=minutes), **kw
    )


async def app(
    session: AsyncSession,
    event: Event,
    trip_id: uuid.UUID,
    status: ApplicationStatus = ApplicationStatus.SEARCHING,
    **kw: Any,
) -> Any:
    user = await make_user(session)
    await make_membership(session, event_id=event.id, user_id=user.id)
    return await make_application(
        session, event_id=event.id, user_id=user.id, trip_id=trip_id, status=status, **kw
    )


async def test_empty_group(api_client: AsyncClient, event: Event) -> None:
    response = await get_board(api_client, event.id)
    assert response.status_code == 200
    assert response.json()["trips"] == []
    assert counters(response.json()["counters"]) == [0, 0, 0, 0, 0, 0]


async def test_order_people_and_cancelled(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    late = await trip(db_session, event, 60)
    early_by_estimate = await trip(db_session, event, 120, estimated_arrival=BASE)
    only_cancelled = await trip(db_session, event, 10)
    a1 = await app(db_session, event, late.id, with_companion=True, created_at=BASE)
    a2 = await app(db_session, event, late.id, created_at=BASE + timedelta(minutes=1))
    await app(db_session, event, late.id, ApplicationStatus.CANCELLED, with_companion=True)
    a3 = await app(db_session, event, early_by_estimate.id, ApplicationStatus.SOLO)
    await app(db_session, event, only_cancelled.id, ApplicationStatus.CANCELLED)

    response = await get_board(api_client, event.id)

    assert response.status_code == 200
    data = response.json()
    assert [t["trip"]["id"] for t in data["trips"]] == [str(early_by_estimate.id), str(late.id)]
    assert [t["people"] for t in data["trips"]] == [1, 3]
    assert data["trips"][0]["application_ids"] == [str(a3.id)]
    assert data["trips"][1]["application_ids"] == [str(a1.id), str(a2.id)]
    assert counters(data["counters"]) == [4, 3, 0, 1, 0, 0]


async def test_trip_status_filter(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    sched = await trip(db_session, event, 0, status=TripStatus.SCHEDULED)
    delayed = await trip(db_session, event, 10, status=TripStatus.DELAYED)
    landed = await trip(db_session, event, 20, status=TripStatus.LANDED)
    await app(db_session, event, sched.id)
    await app(db_session, event, delayed.id, with_companion=True)
    await app(db_session, event, landed.id)

    one = (await get_board(api_client, event.id, params={"trip_status": "delayed"})).json()
    assert [t["trip"]["id"] for t in one["trips"]] == [str(delayed.id)]
    assert counters(one["counters"]) == [2, 2, 0, 0, 0, 0]

    two = (
        await get_board(api_client, event.id, params={"trip_status": ["scheduled", "landed"]})
    ).json()
    assert [t["trip"]["id"] for t in two["trips"]] == [str(sched.id), str(landed.id)]
    assert counters(two["counters"]) == [2, 2, 0, 0, 0, 0]


async def test_unknown_trip_status(api_client: AsyncClient, event: Event) -> None:
    response = await get_board(api_client, event.id, params={"trip_status": "bogus"})
    assert response.status_code == 422


async def test_counters_match_participants(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t1 = await trip(db_session, event, 0)
    t2 = await trip(db_session, event, 30)
    await app(db_session, event, t1.id, with_companion=True, at_meeting_point=True)
    await app(db_session, event, t1.id, ApplicationStatus.SOLO, at_meeting_point=True)
    await app(db_session, event, t2.id, departed_at=BASE)
    await app(db_session, event, t2.id, ApplicationStatus.CANCELLED, with_companion=True)

    board = (await get_board(api_client, event.id)).json()["counters"]
    participants = await api_client.get(
        f"/events/{event.id}/admin/participants", headers=auth_headers(ADMIN_TELEGRAM_ID)
    )
    assert board == participants.json()["counters"]


async def test_other_group_trips_hidden(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    other = await make_event(db_session)
    foreign = await trip(db_session, other)
    await app(db_session, other, foreign.id)

    response = await get_board(api_client, event.id)
    assert response.json()["trips"] == []
    assert counters(response.json()["counters"]) == [0, 0, 0, 0, 0, 0]


async def test_access(api_client: AsyncClient, db_session: AsyncSession, event: Event) -> None:
    member = await make_user(db_session, telegram_id=USER_TELEGRAM_ID)
    await make_membership(db_session, event_id=event.id, user_id=member.id)
    await make_user(db_session, telegram_id=777)

    assert (await get_board(api_client, event.id, None)).status_code == 401
    assert (await get_board(api_client, event.id, USER_TELEGRAM_ID)).status_code == 403
    assert (await get_board(api_client, event.id, 777)).status_code == 404
    assert (await get_board(api_client, MISSING_ID)).status_code == 404


async def _count_queries(engine: AsyncEngine, client: AsyncClient, event: Event) -> int:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa_event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await get_board(client, event.id)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200
    return len(statements)


async def _group(session: AsyncSession, admin: User, size: int) -> Event:
    group = await make_event(session, admin_id=admin.id)
    for i in range(size):
        t = await trip(session, group, i * 5)
        await app(session, group, t.id, with_companion=i % 2 == 0)
    return group


async def test_query_count_does_not_grow(
    api_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine, admin: User
) -> None:
    small = await _group(db_session, admin, 2)
    large = await _group(db_session, admin, 6)

    await _count_queries(db_engine, api_client, small)  # прогрев аутентификации
    few = await _count_queries(db_engine, api_client, small)
    many = await _count_queries(db_engine, api_client, large)

    assert few == many
