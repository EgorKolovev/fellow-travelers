"""`flights.apply_update`: данные поставщика в рейс, история и пересчёт."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.flights import ProviderFlight
from app.models import BackgroundJob, Trip, TripSource, TripStatus
from app.services.flights import apply_update
from tests.factories import make_event, make_trip
from tests.helpers.audit_replay import audit_rows

DEP = datetime(2026, 11, 12, 2, 30, tzinfo=UTC)
ARR = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)


def _flight(trip: Trip, **overrides: object) -> ProviderFlight:
    fields: dict[str, object] = {
        "provider_ref": "ref-1",
        "number": trip.number,
        "departure_iata": "SVX",
        "arrival_iata": "SVO",
        "scheduled_departure": trip.scheduled_departure,
        "scheduled_arrival": trip.scheduled_arrival,
        "estimated_arrival": trip.estimated_arrival,
        "status": trip.status,
    }
    fields.update(overrides)
    return ProviderFlight(**fields)  # type: ignore[arg-type]


async def _trip(session: AsyncSession) -> Trip:
    event = await make_event(session)
    return await make_trip(
        session,
        event_id=event.id,
        provider_ref="ref-1",
        source=TripSource.PROVIDER,
        status=TripStatus.SCHEDULED,
        scheduled_departure=DEP,
        scheduled_arrival=ARR,
    )


async def _jobs(session: AsyncSession, trip: Trip) -> list[BackgroundJob]:
    return list(
        await session.scalars(select(BackgroundJob).where(BackgroundJob.event_id == trip.event_id))
    )


async def test_no_changes_writes_nothing(db_session: AsyncSession) -> None:
    trip = await _trip(db_session)

    assert await apply_update(db_session, trip, _flight(trip)) is False

    assert await audit_rows(db_session, trip.event_id) == []
    assert await _jobs(db_session, trip) == []


async def test_estimate_shift_writes_history_and_enqueues_reconcile(
    db_session: AsyncSession,
) -> None:
    trip = await _trip(db_session)
    estimate = ARR + timedelta(minutes=40)

    changed = await apply_update(
        db_session, trip, _flight(trip, estimated_arrival=estimate, status=TripStatus.DELAYED)
    )

    assert changed is True
    assert trip.estimated_arrival == estimate
    assert trip.status == TripStatus.DELAYED
    (row,) = await audit_rows(db_session, trip.event_id)
    assert (row.action, row.reason, row.actor_kind) == ("trip_updated", "provider_update", "system")
    assert row.trip_id == trip.id
    assert row.details == {
        "changed_fields": ["estimated_arrival", "status"],
        "effective_arrival_before": ARR.isoformat(),
        "effective_arrival_after": estimate.isoformat(),
    }
    (job,) = await _jobs(db_session, trip)
    assert job.trip_id == trip.id


async def test_status_only_change_does_not_enqueue_reconcile(db_session: AsyncSession) -> None:
    trip = await _trip(db_session)

    assert await apply_update(db_session, trip, _flight(trip, status=TripStatus.DEPARTED))

    assert len(await audit_rows(db_session, trip.event_id)) == 1
    assert await _jobs(db_session, trip) == []


async def test_provider_overwrites_admin_edit(db_session: AsyncSession) -> None:
    trip = await _trip(db_session)
    trip.source = TripSource.MANUAL
    trip.estimated_arrival = ARR + timedelta(hours=3)  # правка админа
    await db_session.flush()

    changed = await apply_update(db_session, trip, _flight(trip, estimated_arrival=None))

    assert changed is True
    assert trip.estimated_arrival is None
    assert trip.source == TripSource.PROVIDER
    assert len(await _jobs(db_session, trip)) == 1


async def test_cancellation_and_its_reversal_enqueue_reconcile(db_session: AsyncSession) -> None:
    trip = await _trip(db_session)

    assert await apply_update(db_session, trip, _flight(trip, status=TripStatus.CANCELLED))
    (job,) = await _jobs(db_session, trip)
    await db_session.delete(job)
    await db_session.flush()

    assert await apply_update(db_session, trip, _flight(trip, status=TripStatus.SCHEDULED))
    assert len(await _jobs(db_session, trip)) == 1


async def test_arrival_airport_change_enqueues_reconcile(db_session: AsyncSession) -> None:
    trip = await _trip(db_session)

    assert await apply_update(db_session, trip, _flight(trip, arrival_iata="DME"))

    (row,) = await audit_rows(db_session, trip.event_id)
    assert row.details["changed_fields"] == ["arrival_location_id"]
    assert len(await _jobs(db_session, trip)) == 1


async def test_unknown_airport_is_not_applied_and_logged_without_number(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    trip = await _trip(db_session)
    arrival_before = trip.arrival_location_id
    caplog.set_level(logging.WARNING)

    changed = await apply_update(
        db_session,
        trip,
        _flight(trip, arrival_iata="ZZZ", estimated_arrival=ARR + timedelta(minutes=20)),
    )

    assert changed is True  # время применилось, аэропорт нет
    assert trip.arrival_location_id == arrival_before
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ZZZ" in message for message in messages)
    assert all(trip.number not in message for message in messages)
