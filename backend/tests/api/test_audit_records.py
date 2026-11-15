"""Записи истории, которые пишут сценарии заявки и правка рейса (docs/contracts/README.md, §11).

Читаем таблицу `audit_log` напрямую: API чтения истории делает отдельная задача. Каждое действие
идёт через HTTP, как у пользователя, поэтому проверяется и то, что запись попала в ту же
транзакцию, а при ошибке сценария её нет.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Event, TripSource, User
from tests.api.conftest import auth_headers
from tests.api.test_applications import (
    act,
    apply,
    body,
    get_app,
    join,
    marks,
    put_app,
    trip_at,
)
from tests.api.test_trips import patch_trip
from tests.factories import (
    get_location,
    make_application,
    make_event,
    make_membership,
    make_transfer,
    make_trip,
)
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows

# ключи details, разрешённые таблицей в docs/contracts/README.md, §11
ALLOWED_DETAILS = {
    "status",
    "changed_fields",
    "from_status",
    "to_status",
    "at_meeting_point",
    "departed",
    "application_ids",
    "effective_arrival_before",
    "effective_arrival_after",
}


def brief(row: AuditLog) -> tuple[str, str | None, str, uuid.UUID | None, uuid.UUID | None]:
    """action, reason, actor_kind, actor_user_id, subject_user_id."""
    return row.action, row.reason, row.actor_kind, row.actor_user_id, row.subject_user_id


async def rows(session: AsyncSession, event: Event) -> list[AuditLog]:
    return await audit_rows(session, event.id)


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


@pytest.fixture
async def admin_event(db_session: AsyncSession, user: User) -> Event:
    """Группа, где `user` — админ."""
    return await make_event(db_session, admin_id=user.id)


# ---------------------------------------------------------------- PUT


async def test_first_put_writes_application_submitted(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id))

    assert response.status_code == 201
    (row,) = await rows(db_session, event)
    assert brief(row) == ("application_submitted", None, "participant", user.id, user.id)
    assert row.application_id == uuid.UUID(response.json()["id"])
    assert row.details == {"status": "searching"}
    assert (row.transfer_id, row.trip_id) == (None, None)


async def test_first_put_without_search_writes_solo_status(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip_id = await trip_at(db_session, event)

    await put_app(api_client, db_session, event, body(trip_id, seek_companions=False))

    (row,) = await rows(db_session, event)
    assert row.action == "application_submitted"
    assert row.details == {"status": "solo"}


async def test_repeated_put_writes_only_really_changed_fields(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    trip_id = await trip_at(db_session, event)
    other_trip = await trip_at(db_session, event, 30)
    await put_app(api_client, db_session, event, body(trip_id))

    # то же самое ещё раз: ничего не изменилось — ничего не пишем
    await put_app(api_client, db_session, event, body(trip_id))
    assert len(await rows(db_session, event)) == 1

    await put_app(api_client, db_session, event, body(trip_id, baggage_count=1))
    await put_app(
        api_client,
        db_session,
        event,
        body(other_trip, with_companion=True, baggage_count=1, max_wait_minutes=30),
    )

    _, first, second = await rows(db_session, event)
    assert brief(first) == ("application_updated", None, "participant", user.id, user.id)
    assert first.details == {"changed_fields": ["baggage_count"], "status": "searching"}
    assert brief(second) == brief(first)
    assert second.details == {
        "changed_fields": ["with_companion", "max_wait_minutes", "trip_id"],
        "status": "searching",
    }


async def test_put_on_cancelled_writes_status_in_changed_fields(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip_id = await trip_at(db_session, event)
    await put_app(api_client, db_session, event, body(trip_id))
    await act(api_client, db_session, event, "cancel")

    await put_app(api_client, db_session, event, body(trip_id))

    actions = [row.action for row in await rows(db_session, event)]
    assert actions == ["application_submitted", "status_changed", "application_updated"]
    updated = (await rows(db_session, event))[-1]
    assert updated.details == {"changed_fields": ["status"], "status": "searching"}


async def test_put_seek_false_on_searching_writes_status_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip_id = await trip_at(db_session, event)
    await put_app(api_client, db_session, event, body(trip_id))

    await put_app(api_client, db_session, event, body(trip_id, seek_companions=False))

    updated = (await rows(db_session, event))[-1]
    assert updated.action == "application_updated"
    assert updated.details == {"changed_fields": ["status"], "status": "solo"}


async def test_put_that_matches_writes_submitted_before_transfer_created(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User
) -> None:
    first = await apply(api_client, db_session, event, 101)
    second = await apply(api_client, db_session, event, 42, minutes=5)

    submitted_first, submitted_second, created = await rows(db_session, event)
    assert brief(submitted_first)[:4] == ("application_submitted", None, "participant", p2.id)
    assert submitted_first.details == {"status": "searching"}
    # статус в заявке — до подбора; посадку описывает transfer_created
    assert brief(submitted_second)[:4] == ("application_submitted", None, "participant", user.id)
    assert submitted_second.details == {"status": "searching"}
    assert brief(created) == ("transfer_created", "auto_match", "system", None, None)
    assert created.transfer_id == uuid.UUID(second.json()["transfer_id"])
    assert created.details == {"application_ids": [first.json()["id"], second.json()["id"]]}
    await assert_history_matches_transfers(db_session, event.id)


# ---------------------------------------------------------------- go-solo, resume-search, cancel


async def test_go_solo_from_searching_writes_status_changed_once(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    await apply(api_client, db_session, event, 42)

    await act(api_client, db_session, event, "go-solo")
    await act(api_client, db_session, event, "go-solo")  # идемпотентный повтор

    _, row = await rows(db_session, event)
    assert brief(row) == ("status_changed", "go_solo", "participant", user.id, user.id)
    assert row.details == {"from_status": "searching", "to_status": "solo"}
    assert row.application_id is not None
    assert (row.transfer_id, row.trip_id) == (None, None)


async def test_resume_search_from_solo_writes_status_changed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    await apply(api_client, db_session, event, 42, seek_companions=False)

    await act(api_client, db_session, event, "resume-search")
    await act(api_client, db_session, event, "resume-search")  # уже ищем — повтор ничего не пишет

    _, row = await rows(db_session, event)
    assert brief(row) == ("status_changed", "resume_search", "participant", user.id, user.id)
    assert row.details == {"from_status": "solo", "to_status": "searching"}


async def test_resume_search_that_matches_writes_status_then_transfer_created(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 42, seek_companions=False)

    await act(api_client, db_session, event, "resume-search")

    actions = [(row.action, row.reason, row.actor_kind) for row in await rows(db_session, event)]
    assert actions[-2:] == [
        ("status_changed", "resume_search", "participant"),
        ("transfer_created", "auto_match", "system"),
    ]
    await assert_history_matches_transfers(db_session, event.id)


async def test_cancel_writes_status_changed_once_and_resume_from_cancelled(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    await apply(api_client, db_session, event, 42)

    await act(api_client, db_session, event, "cancel")
    await act(api_client, db_session, event, "cancel")  # идемпотентный повтор
    await act(api_client, db_session, event, "resume-search")

    _, cancelled, resumed = await rows(db_session, event)
    assert brief(cancelled) == ("status_changed", "cancel", "participant", user.id, user.id)
    assert cancelled.details == {"from_status": "searching", "to_status": "cancelled"}
    assert brief(resumed) == ("status_changed", "resume_search", "participant", user.id, user.id)
    assert resumed.details == {"from_status": "cancelled", "to_status": "searching"}


async def test_go_solo_from_cancelled_is_recorded_with_real_from_status(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await act(api_client, db_session, event, "cancel")

    await act(api_client, db_session, event, "go-solo")

    last = (await rows(db_session, event))[-1]
    assert last.details == {"from_status": "cancelled", "to_status": "solo"}


# ---------------------------------------------------------------- выход из трансфера


@pytest.mark.parametrize(
    ("action", "reason", "status"),
    [("go-solo", "go_solo", "solo"), ("cancel", "cancel", "cancelled")],
)
async def test_leave_pair_writes_leave_remaining_and_dissolution(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    user: User,
    p2: User,
    action: str,
    reason: str,
    status: str,
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    transfer_id = uuid.UUID((await get_app(api_client, event)).json()["transfer_id"])
    other = (await get_app(api_client, event, 101)).json()

    await act(api_client, db_session, event, action)

    created, left, remaining_left, dissolved = (await rows(db_session, event))[-4:]
    assert created.action == "transfer_created"
    assert brief(left) == ("transfer_left", reason, "participant", user.id, user.id)
    assert left.transfer_id == transfer_id
    assert left.details == {"from_status": "assigned", "to_status": status}
    # оставшегося выводит тот же участник: actor — он, subject — оставшийся
    assert brief(remaining_left) == ("transfer_left", "member_left", "participant", user.id, p2.id)
    assert remaining_left.application_id == uuid.UUID(other["id"])
    assert remaining_left.details == {"from_status": "assigned", "to_status": "searching"}
    assert brief(dissolved)[:4] == ("transfer_dissolved", "member_left", "participant", user.id)
    assert dissolved.subject_user_id is None
    assert dissolved.transfer_id == transfer_id
    assert dissolved.details == {"application_ids": [other["id"]]}
    await assert_history_matches_transfers(db_session, event.id)


async def test_leave_pair_with_waiting_third_writes_new_transfer_with_auto_fill(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101, minutes=10)
    await apply(api_client, db_session, event, 102, minutes=20)  # к паре 42 + 101 не подходит

    await act(api_client, db_session, event, "cancel")

    tail = (await rows(db_session, event))[-4:]
    assert [row.action for row in tail] == [
        "transfer_left",
        "transfer_left",
        "transfer_dissolved",
        "transfer_created",
    ]
    rematch = tail[-1]
    assert brief(rematch) == ("transfer_created", "auto_fill", "system", None, None)
    assert rematch.transfer_id != tail[0].transfer_id
    assert len(rematch.details["application_ids"]) == 2
    await assert_history_matches_transfers(db_session, event.id)


async def test_put_that_joins_writes_transfer_joined_from_system(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 101)
    pair = await apply(api_client, db_session, event, 102)
    third = await apply(api_client, db_session, event, 42)

    submitted, joined = (await rows(db_session, event))[-2:]
    assert brief(submitted)[:4] == ("application_submitted", None, "participant", user.id)
    assert brief(joined) == ("transfer_joined", "auto_match", "system", None, user.id)
    assert joined.application_id == uuid.UUID(third.json()["id"])
    assert joined.transfer_id == uuid.UUID(pair.json()["transfer_id"])
    assert joined.details == {}
    await assert_history_matches_transfers(db_session, event.id)


async def test_resume_search_that_joins_writes_status_then_transfer_joined(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 102)
    await apply(api_client, db_session, event, 42, seek_companions=False)

    await act(api_client, db_session, event, "resume-search")

    actions = [(row.action, row.reason, row.actor_kind) for row in await rows(db_session, event)]
    assert actions[-2:] == [
        ("status_changed", "resume_search", "participant"),
        ("transfer_joined", "auto_match", "system"),
    ]
    await assert_history_matches_transfers(db_session, event.id)


async def test_leave_triple_with_waiting_fourth_writes_leave_then_auto_fill_join(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    p4 = await join(db_session, event, 103)
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    triple = await apply(api_client, db_session, event, 102)
    fourth = await apply(api_client, db_session, event, p4.telegram_id)

    await act(api_client, db_session, event, "go-solo")

    left, joined = (await rows(db_session, event))[-2:]
    assert brief(left) == ("transfer_left", "go_solo", "participant", user.id, user.id)
    assert brief(joined) == ("transfer_joined", "auto_fill", "system", None, p4.id)
    assert joined.application_id == uuid.UUID(fourth.json()["id"])
    assert left.transfer_id == joined.transfer_id == uuid.UUID(triple.json()["transfer_id"])
    await assert_history_matches_transfers(db_session, event.id)


async def test_leave_triple_writes_only_leaver(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    transfer = await make_transfer(db_session, event_id=event.id)
    for person in (user, p2, p3):
        await make_application(
            db_session,
            event_id=event.id,
            user_id=person.id,
            trip_id=await trip_at(db_session, event),
            transfer_id=transfer.id,
            baggage_count=0,
        )

    await act(api_client, db_session, event, "go-solo")

    (row,) = await rows(db_session, event)
    assert brief(row) == ("transfer_left", "go_solo", "participant", user.id, user.id)
    assert row.transfer_id == transfer.id


async def test_failed_scenarios_write_nothing(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    before = len(await rows(db_session, event))
    trip_id = await trip_at(db_session, event)

    locked = await put_app(api_client, db_session, event, body(trip_id))  # 409: назначена группа
    unknown = await put_app(api_client, db_session, event, body(uuid.uuid4()))  # 422: нет рейса

    assert (locked.status_code, unknown.status_code) == (409, 422)
    assert len(await rows(db_session, event)) == before


async def test_failed_put_without_phone_writes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    user.phone = None
    await db_session.flush()
    trip_id = await trip_at(db_session, event)

    response = await put_app(api_client, db_session, event, body(trip_id))

    assert response.status_code == 403
    assert await rows(db_session, event) == []


# ---------------------------------------------------------------- отметки


async def test_meeting_point_mark_writes_marks_changed_only_on_change(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    await apply(api_client, db_session, event, 42)

    await marks(api_client, db_session, event, {"at_meeting_point": False})  # уже False
    await marks(api_client, db_session, event, {"departed": False})  # ничего не меняет
    await marks(api_client, db_session, event, {"at_meeting_point": True})
    await marks(api_client, db_session, event, {"at_meeting_point": True})  # повтор
    await marks(api_client, db_session, event, {"at_meeting_point": False})

    _, on, off = await rows(db_session, event)
    assert brief(on) == ("marks_changed", None, "participant", user.id, user.id)
    assert on.details == {"at_meeting_point": True, "departed": False}
    assert off.details == {"at_meeting_point": False, "departed": False}


async def test_departed_mark_writes_marks_changed_once(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await marks(api_client, db_session, event, {"at_meeting_point": True})

    first = await marks(api_client, db_session, event, {"departed": True})
    again = await marks(api_client, db_session, event, {"departed": True})  # идемпотентный повтор

    assert (first.status_code, again.status_code) == (200, 200)
    _, meeting, departed = await rows(db_session, event)
    assert meeting.details == {"at_meeting_point": True, "departed": False}
    assert departed.action == "marks_changed"
    assert departed.details == {"at_meeting_point": True, "departed": True}


async def test_both_marks_in_one_request_write_one_record(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)

    await marks(api_client, db_session, event, {"at_meeting_point": True, "departed": True})

    (_, row) = await rows(db_session, event)
    assert row.details == {"at_meeting_point": True, "departed": True}


async def test_last_departure_writes_transfer_departed_from_that_participant(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    second = await apply(api_client, db_session, event, 101)
    transfer_id = uuid.UUID(second.json()["transfer_id"])

    await marks(api_client, db_session, event, {"departed": True}, 42)
    assert "transfer_departed" not in [row.action for row in await rows(db_session, event)]
    await marks(api_client, db_session, event, {"departed": True}, 101)
    await marks(api_client, db_session, event, {"departed": True}, 101)  # повтор

    marks_row, departed = (await rows(db_session, event))[-2:]
    assert brief(marks_row)[:2] == ("marks_changed", None)
    assert brief(departed) == ("transfer_departed", "all_departed", "participant", p2.id, None)
    assert departed.transfer_id == transfer_id
    assert departed.details == {}
    assert len(await rows(db_session, event)) == 6
    await assert_history_matches_transfers(db_session, event.id)


async def test_leaving_triple_with_all_remaining_departed_writes_transfer_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    transfer = await make_transfer(db_session, event_id=event.id)
    for person in (user, p2, p3):
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
    before = len(await rows(db_session, event))

    await act(api_client, db_session, event, "cancel", 102)

    left, departed = (await rows(db_session, event))[before:]
    assert brief(left) == ("transfer_left", "cancel", "participant", p3.id, p3.id)
    assert brief(departed) == ("transfer_departed", "all_departed", "participant", p3.id, None)
    assert departed.transfer_id == transfer.id


async def test_failed_marks_write_nothing(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    await apply(api_client, db_session, event, 42)
    await marks(api_client, db_session, event, {"departed": True})
    before = len(await rows(db_session, event))

    conflict = await marks(api_client, db_session, event, {"at_meeting_point": True})

    assert conflict.status_code == 409
    assert len(await rows(db_session, event)) == before


# ---------------------------------------------------------------- рейс


async def test_patch_trip_writes_trip_updated_from_admin(
    api_client: AsyncClient, db_session: AsyncSession, admin_event: Event, user: User
) -> None:
    trip = await make_trip(db_session, event_id=admin_event.id)  # прилёт 05:05 UTC

    response = await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"})

    assert response.status_code == 200
    (row,) = await rows(db_session, admin_event)
    assert brief(row) == ("trip_updated", "admin_edit", "admin", user.id, None)
    assert (row.trip_id, row.application_id, row.transfer_id) == (trip.id, None, None)
    # Шереметьево — UTC+3: 09:15 местного — 06:15 UTC
    assert row.details == {
        "changed_fields": ["estimated_arrival"],
        "effective_arrival_before": "2026-11-12T05:05:00+00:00",
        "effective_arrival_after": "2026-11-12T06:15:00+00:00",
    }


async def test_patch_trip_lists_only_really_changed_fields(
    api_client: AsyncClient, db_session: AsyncSession, admin_event: Event
) -> None:
    trip = await make_trip(db_session, event_id=admin_event.id, source=TripSource.PROVIDER)
    kgd = await get_location(db_session, "KGD")

    await patch_trip(
        api_client,
        trip.id,
        {
            "number": trip.number.lower(),  # после нормализации то же значение
            "scheduled_departure": trip.scheduled_departure.isoformat(),  # то же значение
            "arrival_location_id": str(kgd.id),
            "scheduled_arrival": "2026-11-12T09:00:00",  # Калининград, UTC+2: 07:00 UTC
        },
    )

    (row,) = await rows(db_session, admin_event)
    assert row.details == {
        "changed_fields": ["arrival_location_id", "scheduled_arrival", "source"],
        "effective_arrival_before": "2026-11-12T05:05:00+00:00",
        "effective_arrival_after": "2026-11-12T07:00:00+00:00",
    }


async def test_patch_trip_without_changes_writes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, admin_event: Event
) -> None:
    trip = await make_trip(db_session, event_id=admin_event.id)

    same = await patch_trip(
        api_client,
        trip.id,
        {
            "number": trip.number,
            "departure_location_id": str(trip.departure_location_id),
            "arrival_location_id": str(trip.arrival_location_id),
            "scheduled_arrival": trip.scheduled_arrival.isoformat(),
            "estimated_arrival": None,
        },
    )
    empty = await patch_trip(api_client, trip.id, {})

    assert (same.status_code, empty.status_code) == (200, 200)
    assert await rows(db_session, admin_event) == []


async def test_failed_patch_trip_writes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, admin_event: Event, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=admin_event.id)
    foreign = await make_trip(db_session, event_id=event.id)

    invalid = await patch_trip(api_client, trip.id, {"scheduled_departure": "2026-11-12T20:00:00"})
    forbidden = await patch_trip(api_client, foreign.id, {"number": "SU7"})

    assert (invalid.status_code, forbidden.status_code) == (422, 403)
    assert await rows(db_session, admin_event) == []
    assert await rows(db_session, event) == []


# ---------------------------------------------------------------- общее


async def test_records_take_request_id_from_request_header(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    # request_id в контекст кладёт middleware запроса, сценарий берёт его оттуда
    trip_id = await trip_at(db_session, event)
    headers = {**auth_headers(), "X-Request-ID": "req-abc-123"}

    response = await api_client.put(
        f"/events/{event.id}/my-application", json=body(trip_id), headers=headers
    )

    assert response.headers["X-Request-ID"] == "req-abc-123"
    (row,) = await rows(db_session, event)
    assert (row.request_id, row.job_id) == ("req-abc-123", None)


async def test_records_have_no_personal_data(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User, p2: User, p3: User
) -> None:
    """Полный набор действий; в записях только id, статусы и поля из таблицы контракта."""
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101, minutes=5)
    await marks(api_client, db_session, event, {"at_meeting_point": True}, 42)
    await apply(api_client, db_session, event, 102, minutes=6)
    await act(api_client, db_session, event, "go-solo", 42)
    await act(api_client, db_session, event, "resume-search", 42)
    await act(api_client, db_session, event, "cancel", 101)
    await act(api_client, db_session, event, "cancel", 42)
    await marks(api_client, db_session, event, {"departed": True}, 102)

    history = await rows(db_session, event)
    assert len(history) > 10
    assert {row.action for row in history} >= {
        "application_submitted",
        "status_changed",
        "marks_changed",
        "transfer_created",
        "transfer_joined",
        "transfer_left",
        "transfer_dissolved",
    }
    for row in history:
        assert set(row.details) <= ALLOWED_DETAILS, row.details
        text = json.dumps(
            {column.name: str(getattr(row, column.name)) for column in AuditLog.__table__.columns},
            ensure_ascii=False,
        ).lower()
        for person in (user, p2, p3):
            assert person.phone is not None
            for secret in (person.phone, person.first_name, person.username or ""):
                assert secret == "" or secret.lower() not in text, (secret, text)
    await assert_history_matches_transfers(db_session, event.id)
