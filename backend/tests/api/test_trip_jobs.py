"""PATCH /trips/{id} ставит задание пересчёта трансферов и не ставит его без нужды.

Задание создаётся в транзакции правки: при отказе (422) не остаётся ни правки, ни записи
истории, ни задания. Последний тест проходит весь путь: правка, воркер, история группы.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Event, JobKind, JobStatus, Trip, User
from app.services.jobs import JobOutcome, process_next
from tests.api.conftest import USER_TELEGRAM_ID
from tests.api.test_applications import apply, get_app, join
from tests.api.test_history import get_history, items_of
from tests.api.test_trips import patch_trip
from tests.factories import (
    get_location,
    make_event,
    make_job,
    make_membership,
    make_trip,
    make_user,
)
from tests.helpers.audit_replay import audit_rows
from tests.helpers.jobs import job_count, jobs_of, shared_session_factory

ADMIN_TELEGRAM_ID = 500
MATE_TELEGRAM_ID = 101


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    """Группа, где `user` (telegram 42) — админ."""
    return await make_event(db_session, admin_id=user.id)


async def fresh_trip(session: AsyncSession, trip_id: uuid.UUID) -> Trip:
    stmt = select(Trip).where(Trip.id == trip_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one()


# ---------------------------------------------------------------- постановка задания


async def test_patch_estimated_arrival_enqueues_one_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)

    response = await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"})

    assert response.status_code == 200
    (job,) = await jobs_of(db_session, event.id)
    assert (job.kind, job.status, job.attempts) == (JobKind.RECONCILE_TRIP, JobStatus.PENDING, 0)
    assert (job.trip_id, job.last_error) == (trip.id, None)


async def test_patch_scheduled_arrival_without_estimate_enqueues_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)

    response = await patch_trip(api_client, trip.id, {"scheduled_arrival": "2026-11-12T09:15:00"})

    assert response.status_code == 200
    assert len(await jobs_of(db_session, event.id)) == 1


async def test_patch_scheduled_arrival_behind_estimate_does_not_enqueue(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    # подбор смотрит на расчётное прибытие: расписание под ним на состав не влияет
    trip = await make_trip(
        db_session, event_id=event.id, estimated_arrival=datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    )

    response = await patch_trip(api_client, trip.id, {"scheduled_arrival": "2026-11-12T09:15:00"})

    assert response.status_code == 200
    assert await jobs_of(db_session, event.id) == []


async def test_patch_arrival_airport_enqueues_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    dme = await get_location(db_session, "DME")

    response = await patch_trip(api_client, trip.id, {"arrival_location_id": str(dme.id)})

    assert response.status_code == 200
    (job,) = await jobs_of(db_session, event.id)
    assert job.trip_id == trip.id


async def test_patch_time_and_airport_together_enqueue_one_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    dme = await get_location(db_session, "DME")

    response = await patch_trip(
        api_client,
        trip.id,
        {"arrival_location_id": str(dme.id), "estimated_arrival": "2026-11-12T09:15:00"},
    )

    assert response.status_code == 200
    assert len(await jobs_of(db_session, event.id)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"number": "dp 55"},
        {"scheduled_departure": "2026-11-12T06:00:00"},
        {},
    ],
    ids=["number", "departure", "empty"],
)
async def test_patch_without_arrival_change_does_not_enqueue(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, payload: dict[str, Any]
) -> None:
    trip = await make_trip(db_session, event_id=event.id)

    response = await patch_trip(api_client, trip.id, payload)

    assert response.status_code == 200
    assert await jobs_of(db_session, event.id) == []


async def test_patch_with_same_values_does_not_enqueue(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    same = {"scheduled_arrival": trip.scheduled_arrival.isoformat()}

    response = await patch_trip(api_client, trip.id, same)

    assert response.status_code == 200
    assert await jobs_of(db_session, event.id) == []
    assert [row.action for row in await audit_rows(db_session, event.id)] == []


async def test_consecutive_patches_share_one_pending_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    other = await make_trip(db_session, event_id=event.id)

    await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"})
    await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:30:00"})
    await patch_trip(api_client, other.id, {"estimated_arrival": "2026-11-12T09:45:00"})

    (job,) = await jobs_of(db_session, event.id)
    assert job.trip_id == other.id  # при слиянии trip_id уходит к последней правке
    # правки записаны все: сливается только задание пересчёта
    actions = [row.action for row in await audit_rows(db_session, event.id)]
    assert actions == ["trip_updated"] * 3


async def test_patch_after_failed_job_enqueues_a_new_one(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED, attempts=8)

    await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"})

    statuses = sorted(job.status for job in await jobs_of(db_session, event.id))
    assert statuses == [JobStatus.FAILED, JobStatus.PENDING]


# ---------------------------------------------------------------- отказ откатывает всё


async def test_patch_422_leaves_no_edit_no_record_no_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    other = await make_trip(db_session, event_id=event.id)
    trip_id, taken_number = trip.id, other.number
    before = (trip.number, trip.estimated_arrival, trip.arrival_location_id)

    # правка времени и номера, занятого другим рейсом: дубль отклоняется уже при записи
    response = await patch_trip(
        api_client,
        trip_id,
        {"number": taken_number, "estimated_arrival": "2026-11-12T09:15:00"},
    )

    assert response.status_code == 422
    after = await fresh_trip(db_session, trip_id)
    assert (after.number, after.estimated_arrival, after.arrival_location_id) == before
    assert await jobs_of(db_session, event.id) == []
    assert await audit_rows(db_session, event.id) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"estimated_arrival": "2026-11-12T01:00:00", "scheduled_arrival": "2026-11-12T09:15:00"},
        {"arrival_location_id": str(uuid.uuid4())},
        {"scheduled_arrival": "2026-11-12T01:00:00"},
    ],
    ids=["estimate-before-departure", "unknown-airport", "arrival-before-departure"],
)
async def test_patch_validation_error_leaves_no_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, payload: dict[str, Any]
) -> None:
    trip = await make_trip(db_session, event_id=event.id)

    response = await patch_trip(api_client, trip.id, payload)

    assert response.status_code == 422
    assert await jobs_of(db_session, event.id) == []
    assert await audit_rows(db_session, event.id) == []
    assert await job_count(db_session) == 0


async def test_patch_by_non_admin_leaves_no_job(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    stranger = await make_user(db_session, telegram_id=43, phone="+79990000043")
    await make_membership(db_session, event_id=event.id, user_id=stranger.id)

    response = await patch_trip(
        api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"}, telegram_id=43
    )

    assert response.status_code == 403
    assert await jobs_of(db_session, event.id) == []


# ---------------------------------------------------------------- от правки до истории


async def test_delay_patch_then_worker_excludes_member_and_history_shows_it(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    admin = await make_user(
        db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan", phone="+79990000500"
    )
    event = await make_event(db_session, admin_id=admin.id)
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    await join(db_session, event, MATE_TELEGRAM_ID)
    await apply(api_client, db_session, event, USER_TELEGRAM_ID)
    mate = await apply(api_client, db_session, event, MATE_TELEGRAM_ID)
    assert mate.json()["status"] == "assigned"
    trip_id = mate.json()["trip"]["id"]

    # рейс попутчика задержали на 30 минут при ожидании 15
    patched = await patch_trip(
        api_client,
        trip_id,
        {"estimated_arrival": "2026-11-12T05:35:00+00:00"},
        ADMIN_TELEGRAM_ID,
    )

    assert patched.status_code == 200
    # правка не пересчитывает состав сама: это дело воркера
    assert (await application_of(api_client, event, MATE_TELEGRAM_ID))["status"] == "assigned"
    (job,) = await jobs_of(db_session, event.id)

    outcome = await process_next(shared_session_factory(db_session))

    assert outcome == JobOutcome.DONE
    assert await jobs_of(db_session, event.id) == []
    for telegram_id in (USER_TELEGRAM_ID, MATE_TELEGRAM_ID):
        application = await application_of(api_client, event, telegram_id)
        assert (application["status"], application["transfer_id"]) == ("searching", None)
    items = items_of(await get_history(api_client, event.id))
    left = [item for item in items if item["action"] == "transfer_left"]
    excluded = [item for item in left if item["reason"] == "incompatible_after_change"]
    assert len(excluded) == 1
    assert excluded[0]["actor_kind"] == "system"
    assert excluded[0]["trip_id"] == trip_id  # видно, какая правка рейса вызвала исключение
    assert excluded[0]["actor"] is None
    assert excluded[0]["subject"]["user_id"] != str(admin.id)
    assert items[0]["action"] == "transfer_dissolved"  # самая свежая запись
    # записи, которые написал воркер, помечены заданием
    worker_rows = [row for row in await audit_rows(db_session, event.id) if row.job_id is not None]
    assert {row.job_id for row in worker_rows} == {job.id}
    assert [row.action for row in worker_rows] == [
        "transfer_left",
        "transfer_left",
        "transfer_dissolved",
    ]


async def application_of(client: AsyncClient, event: Event, telegram_id: int) -> dict[str, Any]:
    result: dict[str, Any] = (await get_app(client, event, telegram_id)).json()
    return result


async def test_job_after_a_processed_one_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    trip = await make_trip(db_session, event_id=event.id)
    factory = shared_session_factory(db_session)
    await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"})
    assert await process_next(factory) == JobOutcome.DONE
    await patch_trip(api_client, trip.id, {"estimated_arrival": "2026-11-12T09:30:00"})
    rows_before = len(list((await db_session.scalars(select(AuditLog.id))).all()))

    assert await process_next(factory) == JobOutcome.DONE

    assert await process_next(factory) is None
    assert len(list((await db_session.scalars(select(AuditLog.id))).all())) == rows_before
