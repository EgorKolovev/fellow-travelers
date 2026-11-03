"""Сервис аудита: `record`, `Actor`, `iso`. Запись в сценариях проверяется в других файлах."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActorKind, AuditAction, AuditLog, AuditReason
from app.observability import context
from app.services.audit import REQUEST_ID_MAX_LENGTH, SYSTEM, Actor, iso, record
from tests.factories import make_application, make_event
from tests.helpers.audit_replay import audit_rows


async def test_record_takes_subject_from_application_and_ids_from_context(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    application = await make_application(db_session, event_id=event.id)
    transfer_id, trip_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    request_token = context.request_id.set("req-42")
    job_token = context.job_id.set(job_id)
    try:
        record(
            db_session,
            event_id=event.id,
            action=AuditAction.TRANSFER_LEFT,
            actor=Actor(ActorKind.PARTICIPANT, application.user_id),
            reason=AuditReason.GO_SOLO,
            application=application,
            transfer_id=transfer_id,
            trip_id=trip_id,
            details={"from_status": "assigned", "to_status": "solo"},
        )
    finally:
        context.request_id.reset(request_token)
        context.job_id.reset(job_token)
    await db_session.flush()

    (row,) = await audit_rows(db_session, event.id)
    assert row.action == AuditAction.TRANSFER_LEFT
    assert row.reason == AuditReason.GO_SOLO
    assert row.actor_kind == ActorKind.PARTICIPANT
    assert row.actor_user_id == application.user_id
    assert row.subject_user_id == application.user_id
    assert row.application_id == application.id
    assert (row.transfer_id, row.trip_id) == (transfer_id, trip_id)
    assert (row.request_id, row.job_id) == ("req-42", job_id)
    assert row.details == {"from_status": "assigned", "to_status": "solo"}
    assert row.occurred_at is not None


async def test_record_without_application_context_and_details(db_session: AsyncSession) -> None:
    event = await make_event(db_session)

    record(db_session, event_id=event.id, action=AuditAction.TRANSFER_DEPARTED, actor=SYSTEM)
    await db_session.flush()

    (row,) = await audit_rows(db_session, event.id)
    assert (row.actor_kind, row.actor_user_id) == (ActorKind.SYSTEM, None)
    assert (row.subject_user_id, row.application_id) == (None, None)
    assert (row.reason, row.transfer_id, row.trip_id) == (None, None, None)
    assert (row.request_id, row.job_id) == (None, None)
    assert row.details == {}


async def test_record_only_adds_to_session(db_session: AsyncSession) -> None:
    event = await make_event(db_session)

    record(db_session, event_id=event.id, action=AuditAction.TRIP_UPDATED, actor=SYSTEM)

    (pending,) = [obj for obj in db_session.new if isinstance(obj, AuditLog)]
    assert pending.id is None  # flush не вызывался: id выдаёт БД при вставке


async def test_record_copies_details(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    details: dict[str, object] = {"changed_fields": ["number"]}

    record(
        db_session,
        event_id=event.id,
        action=AuditAction.TRIP_UPDATED,
        actor=SYSTEM,
        details=details,
    )
    details["changed_fields"] = ["forged"]
    details["extra"] = True
    await db_session.flush()

    (row,) = await audit_rows(db_session, event.id)
    assert row.details == {"changed_fields": ["number"]}


async def test_record_cuts_request_id_to_column_length(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    token = context.request_id.set("x" * (REQUEST_ID_MAX_LENGTH + 40))
    try:
        record(db_session, event_id=event.id, action=AuditAction.TRIP_UPDATED, actor=SYSTEM)
    finally:
        context.request_id.reset(token)
    await db_session.flush()

    (row,) = await audit_rows(db_session, event.id)
    assert row.request_id == "x" * REQUEST_ID_MAX_LENGTH


def test_actor_is_immutable_value() -> None:
    user_id = uuid.uuid4()

    assert Actor(ActorKind.ADMIN, user_id) == Actor(ActorKind.ADMIN, user_id)
    assert Actor(ActorKind.SYSTEM, None) == SYSTEM
    with pytest.raises(dataclasses.FrozenInstanceError):
        SYSTEM.user_id = user_id  # type: ignore[misc]


def test_iso_is_utc_with_offset_for_any_zone() -> None:
    moscow = timezone(timedelta(hours=3))

    assert iso(datetime(2026, 11, 12, 10, 5, tzinfo=moscow)) == "2026-11-12T07:05:00+00:00"
    assert iso(datetime(2026, 11, 12, 7, 5, tzinfo=UTC)) == "2026-11-12T07:05:00+00:00"
