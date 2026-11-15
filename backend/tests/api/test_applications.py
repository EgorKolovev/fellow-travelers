import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    Event,
    Transfer,
    TransferOrigin,
    TransferStatus,
    User,
)
from app.observability.metrics import MatchTrigger
from app.services import applications as applications_service
from app.services import matching as matching_service
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.factories import (
    get_location,
    make_application,
    make_event,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.contract import assert_contract

MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"
APP_PATH = "/events/{event_id}/my-application"
BASE_ARRIVAL = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)

# user (42) — основной участник, вспомогательные участники — 100+
ACTIONS = ("go-solo", "resume-search", "cancel")


# ---------------------------------------------------------------- хелперы


async def trip_at(
    session: AsyncSession, event: Event, minutes: int = 0, arrival: str | None = None
) -> uuid.UUID:
    kw: dict[str, Any] = {"scheduled_arrival": BASE_ARRIVAL + timedelta(minutes=minutes)}
    if arrival is not None:
        kw["arrival_location_id"] = (await get_location(session, arrival)).id
    return (await make_trip(session, event_id=event.id, **kw)).id


async def join(session: AsyncSession, event: Event, telegram_id: int, **kw: Any) -> User:
    kw.setdefault("phone", f"+7999{telegram_id:07d}")
    person = await make_user(session, telegram_id=telegram_id, **kw)
    await make_membership(session, event_id=event.id, user_id=person.id)
    return person


def body(trip_id: uuid.UUID, **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trip_id": str(trip_id),
        "with_companion": False,
        "baggage_count": 0,
        "max_wait_minutes": 15,
    }
    payload.update(over)
    return payload


async def assert_integrity(session: AsyncSession, event_id: uuid.UUID) -> None:
    """assigned только с трансфером; в трансфере ≥ 2 заявок, ≤ 3 пассажиров и ≤ 2 багажа."""
    rows = (
        await session.execute(
            select(
                Application.status,
                Application.transfer_id,
                Application.with_companion,
                Application.baggage_count,
            ).where(Application.event_id == event_id)
        )
    ).all()
    by_transfer: dict[uuid.UUID, list[tuple[int, int]]] = {}
    for status, transfer_id, companion, baggage in rows:
        assert (status == "assigned") == (transfer_id is not None)
        if transfer_id is not None:
            by_transfer.setdefault(transfer_id, []).append((2 if companion else 1, baggage))
    transfer_ids = set(
        (await session.scalars(select(Transfer.id).where(Transfer.event_id == event_id))).all()
    )
    assert transfer_ids == set(by_transfer), "трансфер без заявок или заявка в чужом трансфере"
    for members in by_transfer.values():
        assert len(members) >= 2
        assert sum(p for p, _ in members) <= 3
        assert sum(b for _, b in members) <= 2


async def put_app(
    client: AsyncClient,
    session: AsyncSession,
    event: Event,
    payload: dict[str, Any],
    telegram_id: int = USER_TELEGRAM_ID,
) -> Response:
    response = await client.put(
        f"/events/{event.id}/my-application", json=payload, headers=auth_headers(telegram_id)
    )
    assert_contract(response, "PUT", APP_PATH)
    await assert_integrity(session, event.id)
    return response


async def get_app(
    client: AsyncClient, event: Event, telegram_id: int = USER_TELEGRAM_ID
) -> Response:
    response = await client.get(
        f"/events/{event.id}/my-application", headers=auth_headers(telegram_id)
    )
    assert_contract(response, "GET", APP_PATH)
    return response


async def act(
    client: AsyncClient,
    session: AsyncSession,
    event: Event,
    action: str,
    telegram_id: int = USER_TELEGRAM_ID,
) -> Response:
    response = await client.post(
        f"/events/{event.id}/my-application/{action}", headers=auth_headers(telegram_id)
    )
    assert_contract(response, "POST", f"{APP_PATH}/{action}")
    await assert_integrity(session, event.id)
    return response


async def marks(
    client: AsyncClient,
    session: AsyncSession,
    event: Event,
    payload: dict[str, Any],
    telegram_id: int = USER_TELEGRAM_ID,
) -> Response:
    response = await client.put(
        f"/events/{event.id}/my-application/marks", json=payload, headers=auth_headers(telegram_id)
    )
    assert_contract(response, "PUT", f"{APP_PATH}/marks")
    await assert_integrity(session, event.id)
    return response


async def apply(
    client: AsyncClient,
    session: AsyncSession,
    event: Event,
    telegram_id: int,
    minutes: int = 0,
    **over: Any,
) -> Response:
    """Заявка участника на новый рейс прилёта через `minutes` после базового."""
    trip_id = await trip_at(session, event, minutes, over.pop("arrival", None))
    return await put_app(client, session, event, body(trip_id, **over), telegram_id)


async def transfer_count(session: AsyncSession, event: Event) -> int:
    result = await session.scalar(
        select(func.count()).select_from(Transfer).where(Transfer.event_id == event.id)
    )
    assert result is not None
    return result


async def transfer_status(session: AsyncSession, transfer_id: uuid.UUID) -> TransferStatus:
    result = await session.scalar(
        select(Transfer.status)
        .where(Transfer.id == transfer_id)
        .execution_options(populate_existing=True)
    )
    assert result is not None
    return result


def error_code(response: Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    """Группа с чужим админом; `user` (telegram 42, с телефоном) — её участник."""
    created = await make_event(db_session)
    await make_membership(db_session, event_id=created.id, user_id=user.id)
    return created


@pytest.fixture
async def p2(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 101)


@pytest.fixture
async def p3(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 102)


# ---------------------------------------------------------------- доступ


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", ""),
        ("PUT", ""),
        ("POST", "/go-solo"),
        ("POST", "/resume-search"),
        ("POST", "/cancel"),
        ("PUT", "/marks"),
    ],
)
async def test_requires_auth(api_client: AsyncClient, method: str, suffix: str) -> None:
    response = await api_client.request(method, f"/events/{MISSING_ID}/my-application{suffix}")

    assert response.status_code == 401
    assert_contract(response, method, f"{APP_PATH}{suffix}")


async def test_stranger_gets_404(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, other_user: User
) -> None:
    trip_id = await trip_at(db_session, event)
    headers = auth_headers(43)
    url = f"/events/{event.id}/my-application"

    responses = [
        (await api_client.get(url, headers=headers), "GET", ""),
        (await api_client.put(url, json=body(trip_id), headers=headers), "PUT", ""),
        (await api_client.post(f"{url}/go-solo", headers=headers), "POST", "/go-solo"),
        (await api_client.post(f"{url}/resume-search", headers=headers), "POST", "/resume-search"),
        (await api_client.post(f"{url}/cancel", headers=headers), "POST", "/cancel"),
        (
            await api_client.put(f"{url}/marks", json={"departed": True}, headers=headers),
            "PUT",
            "/marks",
        ),
    ]

    for response, method, suffix in responses:
        assert response.status_code == 404
        assert_contract(response, method, f"{APP_PATH}{suffix}")
    assert await transfer_count(db_session, event) == 0


async def test_admin_without_membership(
    api_client: AsyncClient, db_session: AsyncSession, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)
    trip_id = await trip_at(db_session, event)
    headers = auth_headers(43)
    url = f"/events/{event.id}/my-application"

    put = await api_client.put(url, json=body(trip_id), headers=headers)
    assert put.status_code == 403
    assert error_code(put) == "FORBIDDEN"
    assert put.json()["error"]["message"] == "Чтобы подать заявку, вступите в группу по приглашению"
    assert_contract(put, "PUT", APP_PATH)
    assert (await get_app_status(api_client, event, 43)) == 404
    for method, suffix in [
        ("POST", "/go-solo"),
        ("POST", "/resume-search"),
        ("POST", "/cancel"),
        ("PUT", "/marks"),
    ]:
        response = await api_client.request(
            method, f"{url}{suffix}", headers=headers, json={"departed": True}
        )
        assert response.status_code == 403
        assert error_code(response) == "FORBIDDEN"
        assert_contract(response, method, f"{APP_PATH}{suffix}")


async def test_admin_without_membership_and_phone_gets_forbidden(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, telegram_id=60, phone=None)
    event = await make_event(db_session, admin_id=admin.id)
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id), 60)

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN"


async def get_app_status(client: AsyncClient, event: Event, telegram_id: int) -> int:
    response = await get_app(client, event, telegram_id)
    return response.status_code


async def test_member_without_application_gets_404(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    response = await get_app(api_client, event)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Заявка не найдена"

    for action in ACTIONS:
        response = await act(api_client, db_session, event, action)
        assert response.status_code == 404
        assert error_code(response) == "NOT_FOUND"
    response = await marks(api_client, db_session, event, {"departed": True})
    assert response.status_code == 404


# ---------------------------------------------------------------- PUT


async def test_put_without_phone(api_client: AsyncClient, db_session: AsyncSession) -> None:
    created = await make_event(db_session)
    no_phone = await join(db_session, created, 50, phone=None)
    trip_id = await trip_at(db_session, created)

    response = await put_app(api_client, db_session, created, body(trip_id), 50)

    assert response.status_code == 403
    assert error_code(response) == "PHONE_REQUIRED"
    assert response.json()["error"]["message"] == (
        "Чтобы отправить заявку, поделитесь телефоном в боте"
    )
    count = await db_session.scalar(
        select(func.count()).select_from(Application).where(Application.user_id == no_phone.id)
    )
    assert count == 0


async def test_put_trip_of_other_event(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    foreign = await make_trip(db_session)

    response = await put_app(api_client, db_session, event, body(foreign.id))

    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"] == {"trip_id": "Рейс не найден"}


async def test_put_unknown_trip(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    response = await put_app(api_client, db_session, event, body(uuid.UUID(MISSING_ID)))

    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"] == {"trip_id": "Рейс не найден"}


async def test_put_creates_then_updates(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    trip_id = await trip_at(db_session, event)

    created = await put_app(api_client, db_session, event, body(trip_id, baggage_count=1))

    assert created.status_code == 201
    data = created.json()
    assert data["status"] == "searching"
    assert data["transfer_id"] is None
    assert data["passengers"] == 1
    assert data["event_id"] == str(event.id)
    assert data["trip"]["id"] == str(trip_id)
    assert data["trip"]["arrival_location"]["code"] == "SVO"
    assert data["at_meeting_point"] is False
    assert data["departed_at"] is None

    other_trip = await trip_at(db_session, event, 30)
    updated = await put_app(
        api_client,
        db_session,
        event,
        body(other_trip, with_companion=True, baggage_count=2, max_wait_minutes=30),
    )

    assert updated.status_code == 200
    data2 = updated.json()
    assert data2["id"] == data["id"]
    assert data2["trip"]["id"] == str(other_trip)
    assert data2["passengers"] == 2
    assert data2["baggage_count"] == 2
    assert data2["max_wait_minutes"] == 30
    count = await db_session.scalar(
        select(func.count()).select_from(Application).where(Application.user_id == user.id)
    )
    assert count == 1
    assert (await get_app(api_client, event)).json()["id"] == data["id"]


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"max_wait_minutes": 1441}, "max_wait_minutes"),
        ({"baggage_count": 3}, "baggage_count"),
        ({"baggage_count": -1}, "baggage_count"),
        ({"extra": 1}, "extra"),
        ({"with_companion": "maybe"}, "with_companion"),
    ],
)
async def test_put_validation(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    override: dict[str, Any],
    field: str,
) -> None:
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id, **override))

    assert response.status_code == 422
    assert field in response.json()["error"]["details"]["fields"]


@pytest.mark.parametrize("minutes", [0, 45, 90, 1440])
async def test_put_max_wait_minutes_in_range_returned_as_is(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, minutes: int
) -> None:
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id, max_wait_minutes=minutes))

    assert response.status_code == 201
    assert response.json()["max_wait_minutes"] == minutes
    assert (await get_app(api_client, event)).json()["max_wait_minutes"] == minutes


async def test_put_max_wait_minutes_update_to_non_legacy_value(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip_id = await trip_at(db_session, event)
    await put_app(api_client, db_session, event, body(trip_id, max_wait_minutes=15))

    response = await put_app(api_client, db_session, event, body(trip_id, max_wait_minutes=45))

    assert response.status_code == 200
    assert response.json()["max_wait_minutes"] == 45
    assert (await get_app(api_client, event)).json()["max_wait_minutes"] == 45


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(1441, "Не больше 1440", id="above-max"),
        pytest.param(-1, "Не меньше 0", id="negative"),
        pytest.param("15", "Неверный тип значения", id="string"),
        pytest.param(True, "Неверный тип значения", id="bool"),
        pytest.param(15.0, "Неверный тип значения", id="float"),
        pytest.param(None, "Неверный тип значения", id="null"),
    ],
)
async def test_put_max_wait_minutes_invalid(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, value: Any, message: str
) -> None:
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id, max_wait_minutes=value))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == {"max_wait_minutes": message}


async def test_put_missing_fields(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    response = await put_app(api_client, db_session, event, {})

    assert response.status_code == 422
    assert set(response.json()["error"]["details"]["fields"]) == {
        "trip_id",
        "with_companion",
        "baggage_count",
        "max_wait_minutes",
    }


# ---------------------------------------------------------------- подбор


async def test_two_compatible_are_assigned(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    first = await apply(api_client, db_session, event, 42)
    second = await apply(api_client, db_session, event, 101, minutes=5)

    assert first.status_code == 201
    assert first.json()["status"] == "searching"
    assert second.status_code == 201
    assert second.json()["status"] == "assigned"
    transfer_id = second.json()["transfer_id"]
    assert transfer_id is not None
    mine = (await get_app(api_client, event)).json()
    assert mine["status"] == "assigned"
    assert mine["transfer_id"] == transfer_id
    assert await transfer_count(db_session, event) == 1
    transfer = await db_session.get(Transfer, uuid.UUID(transfer_id))
    assert transfer is not None
    assert (await transfer_status(db_session, transfer.id)) == TransferStatus.ACTIVE


async def test_example_a_b(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    # А: 10:00, ждёт 15 мин; Б: 10:10, ждёт 0
    await apply(api_client, db_session, event, 42, minutes=0, max_wait_minutes=15)
    second = await apply(api_client, db_session, event, 101, minutes=10, max_wait_minutes=0)

    assert second.json()["status"] == "assigned"
    assert (await get_app(api_client, event)).json()["status"] == "assigned"


async def test_incompatible_wait_stays_searching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42, minutes=0, max_wait_minutes=0)
    second = await apply(api_client, db_session, event, 101, minutes=10, max_wait_minutes=15)

    assert second.json()["status"] == "searching"
    assert (await get_app(api_client, event)).json()["status"] == "searching"
    assert await transfer_count(db_session, event) == 0


async def test_four_passengers_not_assembled(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42, with_companion=True)
    second = await apply(api_client, db_session, event, 101, with_companion=True)

    assert second.json()["status"] == "searching"
    assert await transfer_count(db_session, event) == 0


async def test_companion_counts_as_two_passengers(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42, with_companion=True)
    second = await apply(api_client, db_session, event, 101)
    third = await apply(api_client, db_session, event, 102)

    assert second.json()["status"] == "assigned"  # 2 + 1 = 3 пассажира
    assert second.json()["passengers"] == 1
    assert third.json()["status"] == "searching"


async def test_baggage_limit(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42, baggage_count=2)
    second = await apply(api_client, db_session, event, 101, baggage_count=1)

    assert second.json()["status"] == "searching"


async def test_different_arrival_airports_not_mixed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    second = await apply(api_client, db_session, event, 101, arrival="OVB")

    assert second.json()["status"] == "searching"
    assert second.json()["trip"]["arrival_location"]["code"] == "OVB"


async def test_other_event_not_mixed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    other_event = await make_event(db_session)
    stranger = await join(db_session, other_event, 103)
    await apply(api_client, db_session, other_event, stranger.telegram_id)
    await make_membership(db_session, event_id=other_event.id, user_id=user.id)

    response = await apply(api_client, db_session, event, 42)

    assert response.json()["status"] == "searching"
    assert await transfer_count(db_session, event) == 0
    assert await transfer_count(db_session, other_event) == 0


async def test_departed_not_matched(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    assert (await marks(api_client, db_session, event, {"departed": True})).status_code == 200

    second = await apply(api_client, db_session, event, 101)

    assert second.json()["status"] == "searching"
    assert (await get_app(api_client, event)).json()["status"] == "searching"


async def members_of(session: AsyncSession, transfer_id: str) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.transfer_id == uuid.UUID(transfer_id))
    )
    assert result is not None
    return result


async def test_third_joins_existing_pair(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    pair = await apply(api_client, db_session, event, 101)
    transfer_id = pair.json()["transfer_id"]

    third = await apply(api_client, db_session, event, 102)

    assert third.status_code == 201
    assert third.json()["status"] == "assigned"
    assert third.json()["transfer_id"] == transfer_id
    for telegram_id in (42, 101):
        assert (await get_app(api_client, event, telegram_id)).json()["transfer_id"] == transfer_id
    assert await transfer_count(db_session, event) == 1
    assert await members_of(db_session, transfer_id) == 3
    joined = [
        row for row in await audit_rows(db_session, event.id) if row.action == "transfer_joined"
    ]
    assert [(row.reason, row.actor_kind, str(row.transfer_id)) for row in joined] == [
        ("auto_match", "system", transfer_id)
    ]
    assert joined[0].application_id == uuid.UUID(third.json()["id"])
    await assert_history_matches_transfers(db_session, event.id)


async def test_fourth_waits_for_full_transfer_then_pairs_with_fifth(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 102)  # третий: машина полная
    fourth = await join(db_session, event, 103)
    fifth = await join(db_session, event, 104)

    waiting = await apply(api_client, db_session, event, fourth.telegram_id)
    paired = await apply(api_client, db_session, event, fifth.telegram_id)

    assert waiting.json()["status"] == "searching"
    assert paired.json()["status"] == "assigned"
    assert (
        paired.json()["transfer_id"]
        == (await get_app(api_client, event, 103)).json()["transfer_id"]
    )
    assert await transfer_count(db_session, event) == 2


async def test_no_join_when_member_of_transfer_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    pair = await apply(api_client, db_session, event, 101)
    await marks(api_client, db_session, event, {"departed": True}, 42)

    third = await apply(api_client, db_session, event, 102)

    assert third.json()["status"] == "searching"
    assert third.json()["transfer_id"] is None
    assert await members_of(db_session, pair.json()["transfer_id"]) == 2


async def test_no_join_to_admin_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    transfer = await make_transfer(db_session, event_id=event.id, origin=TransferOrigin.ADMIN)
    for person in (user, p2):
        await make_application(
            db_session,
            event_id=event.id,
            user_id=person.id,
            trip_id=await trip_at(db_session, event),
            transfer_id=transfer.id,
            baggage_count=0,
        )

    third = await apply(api_client, db_session, event, 102)

    assert third.json()["status"] == "searching"
    assert await members_of(db_session, str(transfer.id)) == 2


async def test_no_join_when_companion_does_not_fit(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    pair = await apply(api_client, db_session, event, 101)

    third = await apply(api_client, db_session, event, 102, with_companion=True)  # 2 + 2 = 4

    assert third.json()["status"] == "searching"
    assert await members_of(db_session, pair.json()["transfer_id"]) == 2


async def test_no_join_when_baggage_exceeds_limit(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42, baggage_count=1)
    pair = await apply(api_client, db_session, event, 101, baggage_count=1)

    third = await apply(api_client, db_session, event, 102, baggage_count=1)  # 1 + 1 + 1 = 3 > 2

    assert third.json()["status"] == "searching"
    assert await members_of(db_session, pair.json()["transfer_id"]) == 2


async def test_new_full_group_beats_joining_pair(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 101)
    pair = await apply(api_client, db_session, event, 102)
    # двое ждут и друг с другом совместимы: сервис к такому состоянию не приводит, ставим фабрикой
    waiting = [
        await make_application(
            db_session,
            event_id=event.id,
            trip_id=await trip_at(db_session, event),
            baggage_count=0,
        )
        for _ in range(2)
    ]

    response = await apply(api_client, db_session, event, 42)

    assert response.json()["status"] == "assigned"
    assert response.json()["transfer_id"] != pair.json()["transfer_id"]
    assert await transfer_count(db_session, event) == 2
    assert await members_of(db_session, pair.json()["transfer_id"]) == 2
    assert await members_of(db_session, response.json()["transfer_id"]) == 3
    for application in waiting:
        await db_session.refresh(application)
        assert str(application.transfer_id) == response.json()["transfer_id"]


async def test_third_picks_one_of_two_incompatible(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    # 42 и 101 несовместимы по багажу, поэтому сначала ждут; 102 с багажом 0 садится к одному из них
    await apply(api_client, db_session, event, 42, baggage_count=2)
    await apply(api_client, db_session, event, 101, baggage_count=2)

    third = await apply(api_client, db_session, event, 102, baggage_count=0)

    assert third.json()["status"] == "assigned"
    assert await transfer_count(db_session, event) == 1


# ---------------------------------------------------------------- assigned


async def test_put_when_assigned_is_locked(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    trip_id = await trip_at(db_session, event, 1)

    response = await put_app(api_client, db_session, event, body(trip_id, baggage_count=2))

    assert response.status_code == 409
    assert error_code(response) == "APPLICATION_LOCKED"
    assert response.json()["error"]["message"] == (
        "Попутчики уже назначены. Чтобы изменить заявку, сначала выйдите из трансфера"
    )
    assert (await get_app(api_client, event)).json()["baggage_count"] == 0


# ---------------------------------------------------------------- go-solo / cancel


@pytest.mark.parametrize(("action", "status"), [("go-solo", "solo"), ("cancel", "cancelled")])
async def test_leave_pair_dissolves_transfer(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    action: str,
    status: str,
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)

    response = await act(api_client, db_session, event, action)

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["transfer_id"] is None
    other = (await get_app(api_client, event, 101)).json()
    assert other["status"] == "searching"
    assert other["transfer_id"] is None
    assert await transfer_count(db_session, event) == 0


@pytest.mark.parametrize(("action", "status"), [("go-solo", "solo"), ("cancel", "cancelled")])
async def test_leave_pair_rematches_remaining_with_free(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    p3: User,
    action: str,
    status: str,
) -> None:
    # 102 прилетает на 20 минут позже 42, ждущей 15: к паре 42 + 101 не садится, но с 101 совместима
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101, minutes=10)
    waiting = await apply(api_client, db_session, event, 102, minutes=20)
    assert waiting.json()["status"] == "searching"

    response = await act(api_client, db_session, event, action)

    assert response.json()["status"] == status
    second = (await get_app(api_client, event, 101)).json()
    third = (await get_app(api_client, event, 102)).json()
    assert second["status"] == third["status"] == "assigned"
    assert second["transfer_id"] == third["transfer_id"] is not None
    assert await transfer_count(db_session, event) == 1


@pytest.mark.parametrize(("action", "status"), [("go-solo", "solo"), ("cancel", "cancelled")])
async def test_leave_pair_remaining_joins_other_open_transfer(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    p3: User,
    action: str,
    status: str,
) -> None:
    p4 = await join(db_session, event, 103)
    await apply(api_client, db_session, event, p3.telegram_id)
    other = await apply(api_client, db_session, event, p4.telegram_id)
    other_id = other.json()["transfer_id"]
    # со спутником 42 не помещается в чужой трансфер (2 + 2 пассажира), поэтому ждёт
    await apply(api_client, db_session, event, 42, with_companion=True)
    # пара с 42 размещает две заявки, чужой трансфер — одну, поэтому 101 садится к 42
    pair = await apply(api_client, db_session, event, 101)
    assert pair.json()["transfer_id"] != other_id

    response = await act(api_client, db_session, event, action)

    assert response.json()["status"] == status
    remaining = (await get_app(api_client, event, 101)).json()
    assert remaining["status"] == "assigned"
    assert remaining["transfer_id"] == other_id
    assert await transfer_count(db_session, event) == 1
    assert await members_of(db_session, other_id) == 3


@pytest.mark.parametrize(("action", "status"), [("go-solo", "solo"), ("cancel", "cancelled")])
async def test_leave_triple_fills_place_from_searching(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    p3: User,
    action: str,
    status: str,
) -> None:
    p4 = await join(db_session, event, 103)
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    triple = await apply(api_client, db_session, event, 102)
    transfer_id = triple.json()["transfer_id"]
    waiting = await apply(api_client, db_session, event, p4.telegram_id)
    assert waiting.json()["status"] == "searching"  # машина полная

    response = await act(api_client, db_session, event, action)

    assert response.json()["status"] == status
    assert response.json()["transfer_id"] is None
    newcomer = (await get_app(api_client, event, p4.telegram_id)).json()
    assert (newcomer["status"], newcomer["transfer_id"]) == ("assigned", transfer_id)
    assert await transfer_count(db_session, event) == 1
    assert await members_of(db_session, transfer_id) == 3
    history = await audit_rows(db_session, event.id)
    assert [(row.action, row.reason, row.actor_kind) for row in history[-2:]] == [
        ("transfer_left", action.replace("-", "_"), "participant"),
        ("transfer_joined", "auto_fill", "system"),
    ]
    assert history[-1].application_id == uuid.UUID(newcomer["id"])
    assert history[-1].transfer_id == uuid.UUID(transfer_id)
    await assert_history_matches_transfers(db_session, event.id)


async def test_leave_triple_without_waiting_leaves_place_free(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    triple = await apply(api_client, db_session, event, 102)

    await act(api_client, db_session, event, "go-solo")
    # свободное место осталось: новая совместимая заявка дополняет пару снова
    p4 = await join(db_session, event, 103)
    late = await apply(api_client, db_session, event, p4.telegram_id)

    assert late.json()["status"] == "assigned"
    assert late.json()["transfer_id"] == triple.json()["transfer_id"]


@pytest.mark.parametrize(("action", "status"), [("go-solo", "solo"), ("cancel", "cancelled")])
async def test_leave_triple_keeps_others_together(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    p3: User,
    action: str,
    status: str,
) -> None:
    transfer = await make_transfer(db_session, event_id=event.id)
    for telegram_id in (42, 101, 102):
        person = (
            await db_session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one()
        await make_application(
            db_session,
            event_id=event.id,
            user_id=person.id,
            trip_id=await trip_at(db_session, event),
            transfer_id=transfer.id,
            baggage_count=0,
        )

    response = await act(api_client, db_session, event, action)

    assert response.json()["status"] == status
    for telegram_id in (101, 102):
        other = (await get_app(api_client, event, telegram_id)).json()
        assert other["status"] == "assigned"
        assert other["transfer_id"] == str(transfer.id)
    assert await transfer_count(db_session, event) == 1


async def test_go_solo_is_idempotent_and_from_searching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)

    first = await act(api_client, db_session, event, "go-solo")
    second = await act(api_client, db_session, event, "go-solo")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "solo"


async def test_cancel_resets_meeting_mark_and_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await marks(api_client, db_session, event, {"at_meeting_point": True})

    first = await act(api_client, db_session, event, "cancel")
    second = await act(api_client, db_session, event, "cancel")

    assert first.json()["status"] == "cancelled"
    assert first.json()["at_meeting_point"] is False
    assert second.json() == first.json()


async def test_cancel_then_resume_search_rematches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    await act(api_client, db_session, event, "cancel")

    resumed = await act(api_client, db_session, event, "resume-search")

    assert resumed.status_code == 200
    assert resumed.json()["status"] == "assigned"
    assert (await get_app(api_client, event, 101)).json()["status"] == "assigned"
    assert (
        resumed.json()["transfer_id"]
        == (await get_app(api_client, event, 101)).json()["transfer_id"]
    )
    assert await transfer_count(db_session, event) == 1


async def test_resume_search_without_partners_searches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "cancel")

    resumed = await act(api_client, db_session, event, "resume-search")

    assert resumed.json()["status"] == "searching"


async def test_resume_search_noop_when_searching_or_assigned(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    searching = await apply(api_client, db_session, event, 42)
    again = await act(api_client, db_session, event, "resume-search")
    assert again.json() == searching.json()

    await apply(api_client, db_session, event, 101)
    assigned = await get_app(api_client, event)
    again = await act(api_client, db_session, event, "resume-search")
    assert again.json() == assigned.json()
    assert await transfer_count(db_session, event) == 1


async def test_put_cancelled_returns_to_searching_and_matches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    first = await apply(api_client, db_session, event, 42)
    assert first.json()["status"] == "assigned"
    await act(api_client, db_session, event, "cancel")
    trip_id = await trip_at(db_session, event, 2)

    response = await put_app(api_client, db_session, event, body(trip_id))

    assert response.status_code == 200
    assert response.json()["status"] == "assigned"


# ---------------------------------------------------------------- solo


async def test_put_when_solo_updates_and_stays_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "go-solo")
    await act(api_client, db_session, event, "go-solo")
    other = await join(db_session, event, 110)
    await apply(api_client, db_session, event, other.telegram_id)  # свободная совместимая
    trip_id = await trip_at(db_session, event, 1)

    response = await put_app(
        api_client, db_session, event, body(trip_id, baggage_count=2, with_companion=True)
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "solo"
    assert data["baggage_count"] == 2
    assert data["passengers"] == 2
    assert data["trip"]["id"] == str(trip_id)
    assert data["transfer_id"] is None


async def test_solo_resume_search_matches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 42, max_wait_minutes=0)
    # 101 и 42 собрались; 42 уходит в solo, 101 снова ищет
    await act(api_client, db_session, event, "go-solo")

    resumed = await act(api_client, db_session, event, "resume-search")

    assert resumed.json()["status"] == "assigned"
    assert (await get_app(api_client, event, 101)).json()["status"] == "assigned"


# ---------------------------------------------------------------- marks


async def test_marks_meeting_point_toggle(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)

    on = await marks(api_client, db_session, event, {"at_meeting_point": True})
    assert on.status_code == 200
    assert on.json()["at_meeting_point"] is True
    assert (await get_app(api_client, event)).json()["at_meeting_point"] is True

    off = await marks(api_client, db_session, event, {"at_meeting_point": False})
    assert off.json()["at_meeting_point"] is False


async def test_marks_departed_sets_moment_and_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)

    first = await marks(api_client, db_session, event, {"departed": True})

    assert first.status_code == 200
    moment = first.json()["departed_at"]
    assert moment is not None
    assert datetime.fromisoformat(moment).utcoffset() == timedelta(0)
    assert first.json()["status"] == "searching"  # отъезд статус не меняет

    again = await marks(api_client, db_session, event, {"departed": True})
    assert again.status_code == 200
    assert again.json()["departed_at"] == moment


async def test_marks_departed_false_without_departure_is_noop(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)

    response = await marks(api_client, db_session, event, {"departed": False})

    assert response.status_code == 200
    assert response.json()["departed_at"] is None


@pytest.mark.parametrize(
    "payload",
    [{"departed": False}, {"at_meeting_point": True}, {"at_meeting_point": False}],
)
async def test_marks_after_departure_conflict(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, payload: dict[str, Any]
) -> None:
    await apply(api_client, db_session, event, 42)
    departed = await marks(api_client, db_session, event, {"departed": True})

    response = await marks(api_client, db_session, event, payload)

    assert response.status_code == 409
    assert error_code(response) == "ALREADY_DEPARTED"
    assert (await get_app(api_client, event)).json()["departed_at"] == (
        departed.json()["departed_at"]
    )


async def test_marks_when_cancelled_conflict(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "cancel")

    response = await marks(api_client, db_session, event, {"at_meeting_point": True})

    assert response.status_code == 409
    assert error_code(response) == "APPLICATION_LOCKED"
    assert response.json()["error"]["message"] == "Участие отменено"


@pytest.mark.parametrize(
    "payload",
    [{}, {"extra": True}, {"departed": None}, {"at_meeting_point": "maybe"}],
)
async def test_marks_validation(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, payload: dict[str, Any]
) -> None:
    await apply(api_client, db_session, event, 42)

    response = await marks(api_client, db_session, event, payload)

    assert response.status_code == 422


async def test_all_departed_marks_transfer_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    second = await apply(api_client, db_session, event, 101)
    transfer_id = uuid.UUID(second.json()["transfer_id"])

    await marks(api_client, db_session, event, {"departed": True})
    transfer = await db_session.get(Transfer, transfer_id)
    assert transfer is not None
    assert (await transfer_status(db_session, transfer.id)) == TransferStatus.ACTIVE
    # отметка одного не отмечает остальных
    assert (await get_app(api_client, event, 101)).json()["departed_at"] is None

    await marks(api_client, db_session, event, {"departed": True}, 101)
    assert (await transfer_status(db_session, transfer.id)) == TransferStatus.DEPARTED
    # статус заявки остаётся assigned
    assert (await get_app(api_client, event)).json()["status"] == "assigned"


@pytest.mark.parametrize("action", ACTIONS)
async def test_actions_after_departure_conflict(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, action: str
) -> None:
    await apply(api_client, db_session, event, 42)
    await marks(api_client, db_session, event, {"departed": True})

    response = await act(api_client, db_session, event, action)

    assert response.status_code == 409
    assert error_code(response) == "ALREADY_DEPARTED"
    assert response.json()["error"]["message"] == "Вы уже уехали — изменить поездку нельзя"


async def test_put_after_departure_conflict(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await marks(api_client, db_session, event, {"departed": True})
    trip_id = await trip_at(db_session, event, 1)

    response = await put_app(api_client, db_session, event, body(trip_id))

    assert response.status_code == 409
    assert error_code(response) == "ALREADY_DEPARTED"


async def test_departed_member_leaving_triple_is_blocked_but_others_can_leave(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    """Двое из троих уехали, третий отменил: двое оставшихся уехали, трансфер — departed."""
    transfer = await make_transfer(db_session, event_id=event.id)
    users = [
        (await db_session.execute(select(User).where(User.telegram_id == tid))).scalar_one()
        for tid in (42, 101, 102)
    ]
    for person in users:
        await make_application(
            db_session,
            event_id=event.id,
            user_id=person.id,
            trip_id=await trip_at(db_session, event),
            transfer_id=transfer.id,
            baggage_count=0,
        )
    await marks(api_client, db_session, event, {"departed": True}, 42)
    await marks(api_client, db_session, event, {"departed": True}, 101)

    response = await act(api_client, db_session, event, "cancel", 102)

    assert response.json()["status"] == "cancelled"
    assert (await transfer_status(db_session, transfer.id)) == TransferStatus.DEPARTED


# ---------------------------------------------------------------- блокировка


@pytest.mark.parametrize(
    ("call"),
    [
        lambda c, e, t: c.put(f"/events/{e}/my-application", json=body(t), headers=auth_headers()),
        lambda c, e, t: c.post(f"/events/{e}/my-application/go-solo", headers=auth_headers()),
        lambda c, e, t: c.post(f"/events/{e}/my-application/resume-search", headers=auth_headers()),
        lambda c, e, t: c.post(f"/events/{e}/my-application/cancel", headers=auth_headers()),
        lambda c, e, t: c.put(
            f"/events/{e}/my-application/marks", json={"departed": False}, headers=auth_headers()
        ),
    ],
    ids=["put", "go-solo", "resume-search", "cancel", "marks"],
)
async def test_scenarios_take_event_lock_before_reading_application(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[AsyncClient, uuid.UUID, uuid.UUID], Any],
) -> None:
    trip_id = await trip_at(db_session, event)
    await make_application(
        db_session, event_id=event.id, user_id=(await _me(db_session)).id, trip_id=trip_id
    )
    calls: list[str] = []
    original_lock = matching_service.lock_event
    original_find = applications_service.find_application

    async def spy_lock(session: AsyncSession, event_id: uuid.UUID) -> None:
        calls.append("lock")
        assert event_id == event.id
        await original_lock(session, event_id)

    async def spy_find(*args: Any, **kwargs: Any) -> Any:
        calls.append("find")
        return await original_find(*args, **kwargs)

    monkeypatch.setattr(applications_service, "lock_event", spy_lock)
    monkeypatch.setattr(applications_service, "find_application", spy_find)

    response = await call(api_client, event.id, trip_id)

    assert response.status_code in (200, 201)
    assert calls[0] == "lock"
    assert "find" in calls


@pytest.fixture
def fill_calls(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Сколько раз сценарии заявки вызвали `fill_event` (и для какой группы)."""
    calls: list[uuid.UUID] = []
    original = matching_service.fill_event

    async def spy(
        session: AsyncSession, event_id: uuid.UUID, *, trigger: MatchTrigger
    ) -> matching_service.FillResult:
        assert trigger == MatchTrigger.LEAVE  # go-solo и cancel заполняют освободившееся место
        calls.append(event_id)
        return await original(session, event_id, trigger=trigger)

    monkeypatch.setattr(applications_service, "fill_event", spy)
    return calls


@pytest.mark.parametrize("action", ["go-solo", "cancel"])
async def test_leaving_transfer_calls_fill_event_once(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    fill_calls: list[uuid.UUID],
    action: str,
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)

    await act(api_client, db_session, event, action)

    assert fill_calls == [event.id]


async def test_put_and_resume_search_do_not_call_fill_event(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    fill_calls: list[uuid.UUID],
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    await act(api_client, db_session, event, "go-solo", 101)  # выходит из трансфера: один вызов
    fill_calls.clear()

    await apply(api_client, db_session, event, 101, minutes=1, seek_companions=False)
    resumed = await act(api_client, db_session, event, "resume-search", 101)

    assert resumed.json()["status"] == "assigned"  # подбор сработал, но fill_event не вызывался
    assert fill_calls == []


async def _me(session: AsyncSession) -> User:
    return (
        await session.execute(select(User).where(User.telegram_id == USER_TELEGRAM_ID))
    ).scalar_one()


# ---------------------------------------------------------------- seek_companions


async def test_put_seek_false_creates_solo_without_matching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)

    response = await apply(api_client, db_session, event, 42, seek_companions=False)

    assert response.status_code == 201
    assert response.json()["status"] == "solo"
    assert response.json()["transfer_id"] is None
    assert (await get_app(api_client, event, 101)).json()["status"] == "searching"
    assert await transfer_count(db_session, event) == 0


async def test_put_seek_false_from_searching_makes_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    trip_id = await trip_at(db_session, event, 0)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=False))

    assert response.status_code == 200
    assert response.json()["status"] == "solo"


async def test_put_seek_true_from_solo_searches_and_matches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 42, seek_companions=False)
    trip_id = await trip_at(db_session, event, 0)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=True))

    assert response.status_code == 200
    assert response.json()["status"] == "assigned"
    assert response.json()["transfer_id"] is not None
    assert await transfer_count(db_session, event) == 1


async def test_put_seek_true_from_cancelled_searches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "cancel")
    trip_id = await trip_at(db_session, event, 0)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=True))

    assert response.json()["status"] == "searching"


async def test_put_seek_false_from_cancelled_makes_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "cancel")
    trip_id = await trip_at(db_session, event, 0)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=False))

    assert response.json()["status"] == "solo"


async def test_put_without_seek_keeps_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42, seek_companions=False)
    trip_id = await trip_at(db_session, event, 0)

    response = await put_app(api_client, db_session, event, body(trip_id))

    assert response.json()["status"] == "solo"


@pytest.mark.parametrize("seek", [False, True])
async def test_put_seek_when_assigned_is_locked(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, seek: bool
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    trip_id = await trip_at(db_session, event, 1)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=seek))

    assert response.status_code == 409
    assert error_code(response) == "APPLICATION_LOCKED"


async def test_put_seek_null_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id, seek_companions=None))

    assert response.status_code == 422
    assert "seek_companions" in response.json()["error"]["details"]["fields"]
