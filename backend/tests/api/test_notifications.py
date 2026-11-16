"""Уведомления по сценариям API и воркера (docs/spec-mvp.md, M-04, Д9, Д10, Д12).

Строки `notifications` читаем напрямую: отправку в Telegram делает T10. Каждое действие идёт через
HTTP или сервис, как в проде, поэтому проверяется и то, что строка попала в ту же транзакцию.
"""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.flights.fake import FakeFlightProvider
from app.flights.provider import ProviderFlight
from app.models import (
    Application,
    ApplicationStatus,
    Event,
    Location,
    Notification,
    NotificationKind,
    Transfer,
    Trip,
    TripSource,
    TripStatus,
    User,
)
from app.services.flight_polling import poll_due_trips
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.api.test_admin_transfers import post_transfer, put_members
from tests.api.test_applications import BASE_ARRIVAL, act, apply, join
from tests.api.test_trips import patch_trip
from tests.factories import (
    make_application,
    make_event,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)
from tests.helpers.jobs import shared_session_factory
from tests.helpers.notifications import deliver_pending

ADMIN_TELEGRAM_ID = 500
K = NotificationKind
PAYLOAD_KEYS = {"event_id", "transfer_id", "trip_id", "effective_arrival", "status", "role"}
# телефон и имя фабричного пользователя `user` (tests/api/conftest.py)
PERSONAL = ("+79991234567", "Ivan", "ivan")


async def rows_of(session: AsyncSession, event: Event) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.event_id == event.id)
        .order_by(Notification.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.scalars(stmt)).all())


async def kinds(session: AsyncSession, event: Event, *, after: int = 0) -> dict[uuid.UUID, str]:
    """Вид сообщения по пользователю для строк с номером больше `after`; больше одного — ошибка."""
    result: dict[uuid.UUID, str] = {}
    for row in await rows_of(session, event):
        if row.id <= after:
            continue
        assert row.user_id not in result, "одному человеку два сообщения за транзакцию"
        result[row.user_id] = row.kind
        assert set(row.payload) <= PAYLOAD_KEYS
        text = json.dumps(row.payload, ensure_ascii=False)
        assert not any(item in text for item in PERSONAL)
    return result


async def last_id(session: AsyncSession, event: Event) -> int:
    rows = await rows_of(session, event)
    return rows[-1].id if rows else 0


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    """Группа с чужим админом; `user` (telegram 42) — её участник."""
    created = await make_event(db_session)
    await make_membership(db_session, event_id=created.id, user_id=user.id)
    return created


@pytest.fixture
async def p2(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 101)


@pytest.fixture
async def p3(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 102)


async def app_at(
    session: AsyncSession,
    group: Event,
    *,
    trip: Trip | None = None,
    transfer: Transfer | None = None,
    user: User | None = None,
    **kw: Any,
) -> Application:
    """Заявка участника группы (без записей истории); без `trip` — на свой рейс."""
    person = user or await make_user(session)
    await make_membership(session, event_id=group.id, user_id=person.id)
    own_trip = trip or await make_trip(
        session,
        event_id=group.id,
        scheduled_arrival=BASE_ARRIVAL,
        scheduled_departure=BASE_ARRIVAL - timedelta(hours=3),
    )
    kw.setdefault("baggage_count", 0)
    if transfer is None:
        kw.setdefault("status", ApplicationStatus.SEARCHING)
    return await make_application(
        session,
        event_id=group.id,
        user_id=person.id,
        trip_id=own_trip.id,
        transfer_id=transfer.id if transfer is not None else None,
        **kw,
    )


# ---------------------------------------------------------------- трансферы


async def test_pair_match_tells_the_waiting_member_but_not_the_author_of_put(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    assert await kinds(db_session, event) == {}  # одиночная заявка сообщений не даёт

    response = await apply(api_client, db_session, event, USER_TELEGRAM_ID)

    assert response.status_code == 201
    # актор записи `transfer_created` — система (user_id пуст), а автор PUT — актор записи
    # `application_submitted` той же транзакции: он видит результат в ответе, ему не пишем
    assert await kinds(db_session, event) == {p2.id: K.TRANSFER_ASSIGNED}


async def test_third_member_joining_tells_the_pair_but_not_the_newcomer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 102)
    await deliver_pending(db_session, event.id)  # прежние сообщения «ушли»: события — новые строки
    before = await last_id(db_session, event)

    await apply(api_client, db_session, event, USER_TELEGRAM_ID)

    assert await kinds(db_session, event, after=before) == {
        p2.id: K.TRANSFER_CHANGED,
        p3.id: K.TRANSFER_CHANGED,
    }


async def test_leaving_a_triple_tells_leaver_nothing_and_the_rest_changed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    for telegram_id in (101, 102, USER_TELEGRAM_ID):
        await apply(api_client, db_session, event, telegram_id)
    await deliver_pending(db_session, event.id)  # прежние сообщения «ушли»: события — новые строки
    before = await last_id(db_session, event)

    response = await act(api_client, db_session, event, "go-solo", 102)

    assert response.status_code == 200
    assert await kinds(db_session, event, after=before) == {
        p2.id: K.TRANSFER_CHANGED,
        user.id: K.TRANSFER_CHANGED,
    }


async def test_dissolving_a_pair_gives_the_remaining_member_transfer_left(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    before = await last_id(db_session, event)

    await act(api_client, db_session, event, "cancel", 101)

    # вышедший свою отмену не получает; оставшийся — один `transfer_left`, не ещё и `changed`
    assert await kinds(db_session, event, after=before) == {user.id: K.TRANSFER_LEFT}


# ---------------------------------------------------------------- ручной состав админом


async def _admin_with_event(session: AsyncSession) -> tuple[User, Event]:
    admin = await make_user(session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Admin")
    return admin, await make_event(session, admin_id=admin.id)


async def test_manual_transfer_tells_members_but_not_the_admin(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, group = await _admin_with_event(db_session)
    first = await app_at(db_session, group)
    second = await app_at(db_session, group)
    own = await app_at(db_session, group, user=admin)  # админ участвует со своей заявкой

    response = await post_transfer(api_client, group.id, [first.id, second.id, own.id])

    assert response.status_code == 201
    assert await kinds(db_session, group) == {
        first.user_id: K.TRANSFER_ASSIGNED,
        second.user_id: K.TRANSFER_ASSIGNED,
    }


async def test_moving_member_to_manual_transfer_gives_assigned_and_tells_the_rest(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, group = await _admin_with_event(db_session)
    triple = await make_transfer(db_session, event_id=group.id)
    a, b, c = [await app_at(db_session, group, transfer=triple) for _ in range(3)]
    d = await app_at(db_session, group)
    await db_session.commit()

    response = await post_transfer(api_client, group.id, [c.id, d.id])

    assert response.status_code == 201
    # c вышел из тройки и попал в новый трансфер: итог для него — `assigned`, а не `left`
    assert await kinds(db_session, group) == {
        a.user_id: K.TRANSFER_CHANGED,
        b.user_id: K.TRANSFER_CHANGED,
        c.user_id: K.TRANSFER_ASSIGNED,
        d.user_id: K.TRANSFER_ASSIGNED,
    }


async def test_manual_edit_gives_one_message_per_person_by_priority(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, group = await _admin_with_event(db_session)
    transfer = await make_transfer(db_session, event_id=group.id)
    a, b, c = [await app_at(db_session, group, transfer=transfer) for _ in range(3)]
    d = await app_at(db_session, group)
    await db_session.commit()

    response = await put_members(api_client, group.id, transfer.id, [a.id, b.id, d.id])

    assert response.status_code == 200
    # a и b: «вошёл d» и «вышел c» дали по два `changed` -> одно сообщение; d получает
    # `assigned` (важнее `changed` от выхода c); c выбыл
    assert await kinds(db_session, group) == {
        a.user_id: K.TRANSFER_CHANGED,
        b.user_id: K.TRANSFER_CHANGED,
        c.user_id: K.TRANSFER_LEFT,
        d.user_id: K.TRANSFER_ASSIGNED,
    }
    rows = await rows_of(db_session, group)
    assert len(rows) == 4
    for row in rows:
        assert row.dedupe_key.startswith(f"{row.kind}:")


# ---------------------------------------------------------------- рейсы


async def _flight_group(
    session: AsyncSession, **trip_kw: Any
) -> tuple[Event, Trip, Application, Application]:
    """Группа, рейс R, пассажир рейса R и его попутчик с другого рейса в одном трансфере."""
    _, group = await _admin_with_event(session)
    trip = await make_trip(session, event_id=group.id, **trip_kw)
    transfer = await make_transfer(session, event_id=group.id)
    passenger = await app_at(session, group, transfer=transfer, trip=trip)
    mate = await app_at(session, group, transfer=transfer)
    return group, trip, passenger, mate


async def test_admin_edits_by_ten_minutes_accumulate_against_reported_value(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    group, trip, passenger, mate = await _flight_group(db_session)

    async def edit(local_time: str) -> None:
        response = await patch_trip(
            api_client, trip.id, {"estimated_arrival": local_time}, ADMIN_TELEGRAM_ID
        )
        assert response.status_code == 200

    await edit("2026-11-12T08:15:00")  # +10: тишина, базой становится прежнее прибытие
    assert await rows_of(db_session, group) == []
    fresh = await db_session.get(Trip, trip.id, populate_existing=True)
    assert fresh is not None
    assert fresh.notified_arrival == BASE_ARRIVAL

    await edit("2026-11-12T08:25:00")  # ещё +10: от базы уже 20 минут
    assert await kinds(db_session, group) == {
        passenger.user_id: K.FLIGHT_CHANGED,
        mate.user_id: K.FLIGHT_CHANGED,
    }
    fresh = await db_session.get(Trip, trip.id, populate_existing=True)
    assert fresh is not None
    assert fresh.notified_arrival == BASE_ARRIVAL + timedelta(minutes=20)

    before = await last_id(db_session, group)
    await edit("2026-11-12T08:35:00")  # ещё +10 от сообщённого: тишина
    assert await kinds(db_session, group, after=before) == {}


async def test_admin_edit_by_twenty_minutes_tells_passenger_and_mate_and_sets_baseline(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    group, trip, passenger, mate = await _flight_group(db_session)
    first_departed = await app_at(
        db_session, group, trip=trip, departed_at=BASE_ARRIVAL, status=ApplicationStatus.SOLO
    )
    cancelled = await app_at(db_session, group, trip=trip, status=ApplicationStatus.CANCELLED)

    await patch_trip(
        api_client, trip.id, {"estimated_arrival": "2026-11-12T08:25:00"}, ADMIN_TELEGRAM_ID
    )

    assert await kinds(db_session, group) == {
        passenger.user_id: K.FLIGHT_CHANGED,
        mate.user_id: K.FLIGHT_CHANGED,
    }
    assert first_departed.user_id not in await kinds(db_session, group)
    assert cancelled.user_id not in await kinds(db_session, group)
    by_user = {row.user_id: row for row in await rows_of(db_session, group)}
    common = {
        "event_id": str(group.id),
        "trip_id": str(trip.id),
        "effective_arrival": "2026-11-12T05:25:00+00:00",
        "status": "scheduled",
    }
    assert by_user[passenger.user_id].payload == {**common, "role": "passenger"}
    assert by_user[mate.user_id].payload == {**common, "role": "mate"}
    fresh = await db_session.get(Trip, trip.id, populate_existing=True)
    assert fresh is not None
    assert fresh.notified_arrival == datetime(2026, 11, 12, 5, 25, tzinfo=UTC)

    # ещё на 10 минут от уже сообщённого значения — тишина; ещё на 10 (итого 20) — сообщение
    await deliver_pending(db_session, group.id)
    before = await last_id(db_session, group)
    await patch_trip(
        api_client, trip.id, {"estimated_arrival": "2026-11-12T08:35:00"}, ADMIN_TELEGRAM_ID
    )
    assert await kinds(db_session, group, after=before) == {}
    await patch_trip(
        api_client, trip.id, {"estimated_arrival": "2026-11-12T08:45:00"}, ADMIN_TELEGRAM_ID
    )
    assert set((await kinds(db_session, group, after=before)).values()) == {K.FLIGHT_CHANGED}


@pytest.mark.parametrize(
    ("local_time", "told"),
    [
        ("2026-11-12T08:19:59", False),  # +14:59
        ("2026-11-12T08:20:00", True),  # +15:00
        ("2026-11-12T07:50:00", True),  # -15:00
        ("2026-11-12T07:50:01", False),  # -14:59
    ],
    ids=["plus_14_59", "plus_15_00", "minus_15_00", "minus_14_59"],
)
async def test_arrival_shift_threshold_boundary(
    api_client: AsyncClient, db_session: AsyncSession, local_time: str, told: bool
) -> None:
    """Д10: сообщение при сдвиге на 15 минут и больше от сообщённого значения, в обе стороны."""
    group, trip, passenger, mate = await _flight_group(db_session)  # прибытие 08:05 по Москве

    response = await patch_trip(
        api_client, trip.id, {"estimated_arrival": local_time}, ADMIN_TELEGRAM_ID
    )

    assert response.status_code == 200
    expected = {passenger.user_id: K.FLIGHT_CHANGED, mate.user_id: K.FLIGHT_CHANGED}
    assert await kinds(db_session, group) == (expected if told else {})


async def test_repeated_edit_without_changes_makes_no_messages(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    group, trip, _, _ = await _flight_group(db_session)
    payload = {"estimated_arrival": "2026-11-12T08:25:00"}
    await patch_trip(api_client, trip.id, payload, ADMIN_TELEGRAM_ID)
    count = len(await rows_of(db_session, group))

    await patch_trip(api_client, trip.id, payload, ADMIN_TELEGRAM_ID)

    assert len(await rows_of(db_session, group)) == count == 2


async def test_provider_cancellation_tells_only_passengers_in_worker_session(
    db_session: AsyncSession,
) -> None:
    group, trip, passenger, _ = await _flight_group(
        db_session, provider_ref="fake:SU1", source=TripSource.PROVIDER
    )
    trip.number = "SU1"
    trip.next_poll_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    airport = await _iata(db_session, trip)
    flight = ProviderFlight(
        provider_ref="fake:SU1",
        number="SU1",
        departure_iata=airport[0],
        arrival_iata=airport[1],
        scheduled_departure=trip.scheduled_departure,
        scheduled_arrival=trip.scheduled_arrival,
        estimated_arrival=None,
        status=TripStatus.CANCELLED,
    )
    provider = FakeFlightProvider([flight])

    result = await poll_due_trips(shared_session_factory(db_session), provider)

    assert result.changed == 1
    # отмену рейса получают только его пассажиры; попутчик узнает из пересчёта трансфера
    assert await kinds(db_session, group) == {passenger.user_id: K.FLIGHT_CANCELLED}
    (row,) = await rows_of(db_session, group)
    assert "role" not in row.payload
    # повторный опрос без изменений новых сообщений не создаёт
    fresh = await db_session.get(Trip, trip.id, populate_existing=True)
    assert fresh is not None
    fresh.next_poll_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    again = await poll_due_trips(shared_session_factory(db_session), provider)
    assert again.changed == 0
    assert len(await rows_of(db_session, group)) == 1


async def _iata(session: AsyncSession, trip: Trip) -> tuple[str, str]:
    departure = await session.get(Location, trip.departure_location_id)
    arrival = await session.get(Location, trip.arrival_location_id)
    assert departure is not None
    assert arrival is not None
    return departure.code, arrival.code


# ---------------------------------------------------------------- точки группы


async def test_points_change_tells_participants_except_author_and_cancelled(
    api_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, group = await _admin_with_event(db_session)
    active = await app_at(db_session, group)
    searching = await app_at(db_session, group, status=ApplicationStatus.SEARCHING)
    cancelled = await app_at(db_session, group, status=ApplicationStatus.CANCELLED)
    own = await app_at(db_session, group, user=admin)
    departed = await app_at(
        db_session, group, status=ApplicationStatus.SOLO, departed_at=BASE_ARRIVAL
    )
    await db_session.commit()

    async def patch(payload: dict[str, Any]) -> None:
        response = await api_client.patch(
            f"/events/{group.id}", json=payload, headers=auth_headers(ADMIN_TELEGRAM_ID)
        )
        assert response.status_code == 200

    await patch({"meeting_instruction": "Выходите к колонне 3"})

    rows = await rows_of(db_session, group)
    assert {row.user_id for row in rows} == {active.user_id, searching.user_id}
    assert cancelled.user_id not in {row.user_id for row in rows}
    assert own.user_id not in {row.user_id for row in rows}
    assert departed.user_id not in {row.user_id for row in rows}
    for row in rows:
        assert row.kind == K.POINTS_CHANGED
        assert row.payload == {"event_id": str(group.id)}
        assert row.dedupe_key.startswith(f"points_changed:{group.id}:")

    # поле без точек и правка без изменений сообщений не дают; новая правка — даёт
    await patch({"title": "Новое название"})
    await patch({"meeting_instruction": "Выходите к колонне 3"})
    assert len(await rows_of(db_session, group)) == 2
    # тест идёт в одной транзакции, `now()` в ней не меняется: ключ правки подменяем счётчиком
    ticks = itertools.count()
    monkeypatch.setattr("app.services.events.iso", lambda moment: f"edit-{next(ticks)}")
    # пока первое сообщение не ушло, новая правка склеивается в него: строк по-прежнему две
    await patch({"destination": {"address": "Другой отель", "lat": 55.0, "lon": 37.0}})
    assert len(await rows_of(db_session, group)) == 2
    assert {r.kind for r in await rows_of(db_session, group)} == {K.POINTS_CHANGED}
    # после отправки следующая правка даёт новые строки
    await deliver_pending(db_session, group.id)
    await patch({"destination": {"address": "Третий отель", "lat": 55.1, "lon": 37.1}})
    assert len(await rows_of(db_session, group)) == 4
    assert len(await _all_transfers(db_session, group)) == 0  # трансферы не пересчитываются


async def _all_transfers(session: AsyncSession, group: Event) -> list[Transfer]:
    return list(
        (await session.scalars(select(Transfer).where(Transfer.event_id == group.id))).all()
    )
