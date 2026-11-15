from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    ApplicationStatus,
    AuditLog,
    BackgroundJob,
    Event,
    Membership,
    Transfer,
    Trip,
)
from tests.factories import (
    make_application,
    make_audit_log,
    make_event,
    make_job,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_event_ends_before_starts(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_events_dates_order"):
        await make_event(db_session, starts_on=date(2026, 11, 14), ends_on=date(2026, 11, 12))


async def test_event_one_day_is_allowed(db_session: AsyncSession) -> None:
    await make_event(db_session, starts_on=date(2026, 11, 12), ends_on=date(2026, 11, 12))


async def test_event_duplicate_invite_code(db_session: AsyncSession) -> None:
    await make_event(db_session, invite_code="K7Q2ZP")
    with pytest.raises(IntegrityError, match="uq_events_invite_code"):
        await make_event(db_session, invite_code="K7Q2ZP")


async def test_duplicate_membership(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    user = await make_user(db_session)
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    with pytest.raises(IntegrityError, match="pk_memberships"):
        await make_membership(db_session, event_id=event.id, user_id=user.id)


async def test_baggage_count_above_two(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_applications_baggage_count_range"):
        await make_application(db_session, baggage_count=3)


@pytest.mark.parametrize("minutes", [-1, 1441])
async def test_max_wait_minutes_out_of_range(db_session: AsyncSession, minutes: int) -> None:
    with pytest.raises(IntegrityError, match="ck_applications_max_wait_minutes_range"):
        await make_application(db_session, max_wait_minutes=minutes)


@pytest.mark.parametrize("minutes", [0, 10, 1440])
async def test_max_wait_minutes_in_range_is_allowed(db_session: AsyncSession, minutes: int) -> None:
    application = await make_application(db_session, max_wait_minutes=minutes)
    stored = await db_session.scalar(
        select(Application.max_wait_minutes).where(Application.id == application.id)
    )
    assert stored == minutes


async def test_assigned_without_transfer(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_applications_assigned_has_transfer"):
        await make_application(db_session, status=ApplicationStatus.ASSIGNED)


async def test_transfer_with_searching_status(db_session: AsyncSession) -> None:
    transfer = await make_transfer(db_session)
    with pytest.raises(IntegrityError, match="ck_applications_assigned_has_transfer"):
        await make_application(
            db_session,
            event_id=transfer.event_id,
            transfer_id=transfer.id,
            status=ApplicationStatus.SEARCHING,
        )


async def test_duplicate_application_for_event_and_user(db_session: AsyncSession) -> None:
    first = await make_application(db_session)
    with pytest.raises(IntegrityError, match="uq_applications_event_user"):
        await make_application(
            db_session, event_id=first.event_id, user_id=first.user_id, trip_id=first.trip_id
        )


async def test_trip_arrival_equal_to_departure(db_session: AsyncSession) -> None:
    moment = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="ck_trips_arrival_after_departure"):
        await make_trip(db_session, scheduled_departure=moment, scheduled_arrival=moment)


async def test_trip_arrival_before_departure(db_session: AsyncSession) -> None:
    moment = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="ck_trips_arrival_after_departure"):
        await make_trip(
            db_session, scheduled_departure=moment, scheduled_arrival=moment - timedelta(hours=1)
        )


async def test_duplicate_trip_in_event(db_session: AsyncSession) -> None:
    trip = await make_trip(db_session)
    with pytest.raises(IntegrityError, match="uq_trips_event_mode_number_departure"):
        await make_trip(
            db_session,
            event_id=trip.event_id,
            number=trip.number,
            scheduled_departure=trip.scheduled_departure,
        )


async def test_cannot_delete_trip_with_application(db_session: AsyncSession) -> None:
    application = await make_application(db_session)
    with pytest.raises(IntegrityError, match="fk_applications_trip_id_trips"):
        await db_session.execute(delete(Trip).where(Trip.id == application.trip_id))


async def test_event_delete_cascades(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    member = await make_user(db_session)
    await make_membership(db_session, event_id=event.id, user_id=member.id)
    transfer = await make_transfer(db_session, event_id=event.id)
    trip = await make_trip(db_session, event_id=event.id)
    await make_application(db_session, event_id=event.id, trip_id=trip.id, transfer_id=transfer.id)
    await make_application(db_session, event_id=event.id)
    await make_audit_log(db_session, event_id=event.id)
    await make_job(db_session, event_id=event.id, trip_id=trip.id)

    await db_session.execute(delete(Event).where(Event.id == event.id))

    for model in (Membership, Trip, Transfer, Application, AuditLog, BackgroundJob):
        assert await _count(db_session, model) == 0, model.__name__
