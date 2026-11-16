import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Event, User
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.api.test_applications import BASE_ARRIVAL, act, apply, join, marks
from tests.factories import make_application, make_event, make_membership, make_transfer, make_trip
from tests.helpers.contract import assert_contract

MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"
TRIP_PATH = "/events/{event_id}/my-trip"


async def get_trip(
    client: AsyncClient, event_id: uuid.UUID | str, telegram_id: int = USER_TELEGRAM_ID
) -> Response:
    response = await client.get(f"/events/{event_id}/my-trip", headers=auth_headers(telegram_id))
    assert_contract(response, "GET", TRIP_PATH)
    return response


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    """Группа с чужим админом; `user` (telegram 42) — её участник."""
    created = await make_event(db_session)
    await make_membership(db_session, event_id=created.id, user_id=user.id)
    return created


@pytest.fixture
async def p2(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 101)


# ---------------------------------------------------------------- доступ


async def test_requires_auth(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/events/{MISSING_ID}/my-trip")

    assert response.status_code == 401
    assert_contract(response, "GET", TRIP_PATH)


async def test_stranger_gets_404(api_client: AsyncClient, event: Event, other_user: User) -> None:
    response = await get_trip(api_client, event.id, 43)

    assert response.status_code == 404


async def test_unknown_event_gets_404(api_client: AsyncClient, user: User) -> None:
    assert (await get_trip(api_client, MISSING_ID)).status_code == 404


async def test_admin_without_participation(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    admin = await db_session.get(User, event.admin_id)
    assert admin is not None

    response = await get_trip(api_client, event.id, admin.telegram_id)

    assert response.status_code == 200
    data = response.json()
    assert data["application"] is None
    assert data["transfer"] is None
    assert data["event"]["is_admin"] is True
    assert data["event"]["is_participant"] is False


# ---------------------------------------------------------------- без трансфера


async def test_member_without_application(api_client: AsyncClient, event: Event) -> None:
    response = await get_trip(api_client, event.id)

    assert response.status_code == 200
    data = response.json()
    assert data["application"] is None
    assert data["transfer"] is None
    assert data["event"]["is_participant"] is True
    assert data["event"]["is_admin"] is False
    # pickup_point не задан — посадка в месте сбора
    assert data["taxi"] == {
        "from": {"address": "Терминал D", "lat": 55.97, "lon": 37.41},
        "to": {"address": "Отель", "lat": 55.75, "lon": 37.62},
        "yandex_go_url": data["taxi"]["yandex_go_url"],
    }


async def test_taxi_uses_pickup_point(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    event.pickup_point = {"address": "Парковка P1", "lat": None, "lon": None}
    await db_session.flush()

    data = (await get_trip(api_client, event.id)).json()

    assert data["taxi"]["from"]["address"] == "Парковка P1"
    assert data["taxi"]["to"]["address"] == "Отель"


async def test_searching_has_no_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)

    data = (await get_trip(api_client, event.id)).json()

    assert data["application"]["status"] == "searching"
    assert data["transfer"] is None
    assert data["taxi"]["yandex_go_url"] is not None


# ---------------------------------------------------------------- ссылки


async def test_links_filled_with_coordinates(api_client: AsyncClient, event: Event) -> None:
    data = (await get_trip(api_client, event.id)).json()

    assert data["meeting_map_url"] == "https://yandex.ru/maps/?pt=37.41,55.97&z=17&l=map"
    go = urlsplit(data["taxi"]["yandex_go_url"])
    assert go.netloc == "3.redirect.appmetrica.yandex.com"
    q = parse_qs(go.query)
    # посадка не задана — берётся место сбора
    assert (q["start-lat"], q["start-lon"]) == (["55.97"], ["37.41"])
    assert (q["end-lat"], q["end-lon"]) == (["55.75"], ["37.62"])


async def test_links_use_pickup_point(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    event.pickup_point = {"address": "Парковка P1", "lat": 55.9, "lon": 37.4}
    await db_session.flush()

    data = (await get_trip(api_client, event.id)).json()

    q = parse_qs(urlsplit(data["taxi"]["yandex_go_url"]).query)
    assert (q["start-lat"], q["start-lon"]) == (["55.9"], ["37.4"])
    assert data["meeting_map_url"] == "https://yandex.ru/maps/?pt=37.41,55.97&z=17&l=map"


async def test_links_null_without_coordinates(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    event.meeting_point = {"address": "Терминал D", "lat": None, "lon": None}
    event.destination = {"address": "Отель", "lat": None, "lon": None}
    await db_session.flush()

    data = (await get_trip(api_client, event.id)).json()

    assert data["meeting_map_url"] is None
    assert data["taxi"]["yandex_go_url"] is None


async def test_go_link_null_if_destination_has_no_coordinates(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    event.destination = {"address": "Отель", "lat": None, "lon": None}
    await db_session.flush()

    data = (await get_trip(api_client, event.id)).json()

    assert data["meeting_map_url"] is not None
    assert data["taxi"]["yandex_go_url"] is None


# ---------------------------------------------------------------- трансфер


async def test_two_members_see_same_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID, baggage_count=1)
    await apply(
        api_client, db_session, event, 101, minutes=10, with_companion=True, baggage_count=1
    )

    mine = (await get_trip(api_client, event.id)).json()
    theirs = (await get_trip(api_client, event.id, 101)).json()

    assert mine["application"]["status"] == "assigned"
    transfer = mine["transfer"]
    assert transfer["id"] == theirs["transfer"]["id"] == mine["application"]["transfer_id"]
    assert transfer["status"] == "active"
    assert transfer["passengers_total"] == 3
    assert transfer["baggage_total"] == 2
    assert [m["is_me"] for m in transfer["members"]] == [True, False]
    assert [m["is_me"] for m in theirs["transfer"]["members"]] == [False, True]
    other = transfer["members"][1]
    assert other["contact"]["phone"] == p2.phone
    assert other["contact"]["first_name"] == p2.first_name
    assert other["passengers"] == 2
    assert other["departed_at"] is None
    assert other["at_meeting_point"] is False
    assert transfer["members"][0]["contact"]["username"] == "ivan"


async def test_other_transfer_contacts_hidden(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    await apply(api_client, db_session, event, 101, minutes=5)
    transfer = await make_transfer(db_session, event_id=event.id)
    stranger_a = await join(db_session, event, 103)
    stranger_b = await join(db_session, event, 104)
    for person in (stranger_a, stranger_b):
        await make_application(
            db_session, event_id=event.id, user_id=person.id, transfer_id=transfer.id
        )

    data = (await get_trip(api_client, event.id)).json()

    ids = {m["contact"]["user_id"] for m in data["transfer"]["members"]}
    assert str(stranger_a.id) not in ids
    assert str(stranger_b.id) not in ids
    assert len(ids) == 2
    other = (await get_trip(api_client, event.id, 103)).json()
    assert other["transfer"]["id"] == str(transfer.id)
    assert str(p2.id) not in {m["contact"]["user_id"] for m in other["transfer"]["members"]}


async def test_go_solo_clears_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    await apply(api_client, db_session, event, 101, minutes=5)
    await act(api_client, db_session, event, "go-solo")

    data = (await get_trip(api_client, event.id)).json()

    assert data["application"]["status"] == "solo"
    assert data["transfer"] is None
    assert data["taxi"]["to"]["address"] == "Отель"


async def test_marks_visible_to_fellow_traveler(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    await apply(api_client, db_session, event, 101, minutes=5)
    await marks(api_client, db_session, event, {"at_meeting_point": True}, 101)

    members = (await get_trip(api_client, event.id)).json()["transfer"]["members"]
    assert [m["at_meeting_point"] for m in members] == [False, True]

    await marks(api_client, db_session, event, {"departed": True}, 101)
    members = (await get_trip(api_client, event.id)).json()["transfer"]["members"]
    assert members[0]["departed_at"] is None
    assert members[1]["departed_at"] is not None


async def test_departed_transfer_is_returned(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    await apply(api_client, db_session, event, 101, minutes=5)
    await marks(api_client, db_session, event, {"departed": True}, USER_TELEGRAM_ID)
    await marks(api_client, db_session, event, {"departed": True}, 101)

    transfer = (await get_trip(api_client, event.id)).json()["transfer"]

    assert transfer["status"] == "departed"
    assert all(m["departed_at"] is not None for m in transfer["members"])


async def test_members_ordered_by_arrival(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID, minutes=10)
    await apply(api_client, db_session, event, 101, minutes=0)

    members = (await get_trip(api_client, event.id)).json()["transfer"]["members"]

    assert [m["is_me"] for m in members] == [False, True]
    arrivals = [datetime.fromisoformat(m["effective_arrival"]) for m in members]
    assert arrivals == sorted(arrivals)


async def test_effective_arrival_uses_airport_offset(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    await apply(api_client, db_session, event, 101, minutes=5)

    members = (await get_trip(api_client, event.id)).json()["transfer"]["members"]

    # SVO — Москва, +03:00
    assert members[0]["effective_arrival"] == "2026-11-12T08:05:00+03:00"
    assert datetime.fromisoformat(members[0]["effective_arrival"]) == BASE_ARRIVAL
    assert members[0]["trip_number"].startswith("SU")


# ---------------------------------------------------------------- без N+1


async def _count_get_queries(
    engine: AsyncEngine, client: AsyncClient, event: Event, telegram_id: int
) -> int:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa_event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await get_trip(client, event.id, telegram_id)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert response.json()["transfer"] is not None
    return len(statements)


async def _make_group_transfer(
    session: AsyncSession, event: Event, first_id: int, size: int
) -> None:
    transfer = await make_transfer(session, event_id=event.id)
    for i in range(size):
        person = await join(session, event, first_id + i)
        trip = await make_trip(
            session, event_id=event.id, scheduled_arrival=BASE_ARRIVAL + timedelta(minutes=i)
        )
        await make_application(
            session,
            event_id=event.id,
            user_id=person.id,
            trip_id=trip.id,
            transfer_id=transfer.id,
            baggage_count=0,
        )


async def test_query_count_does_not_grow_with_members(
    api_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine, event: Event
) -> None:
    await _make_group_transfer(db_session, event, 201, 2)
    await _make_group_transfer(db_session, event, 301, 3)

    two = await _count_get_queries(db_engine, api_client, event, 201)
    three = await _count_get_queries(db_engine, api_client, event, 301)

    assert three <= two
