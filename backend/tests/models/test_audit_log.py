"""Таблица audit_log: только добавление (триггер БД), каскад от групп, SET NULL от пользователей."""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Delete, Update, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActorKind,
    AuditAction,
    AuditLog,
    AuditReason,
    Event,
    User,
)
from tests.factories import make_audit_log, make_event, make_user

APPEND_ONLY = "audit_log доступна только для добавления"


async def _count(session: AsyncSession, event_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.event_id == event_id)
        )
    ).scalar_one()


async def _fresh(session: AsyncSession, entry_id: int) -> AuditLog:
    stmt = select(AuditLog).where(AuditLog.id == entry_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one()


def _update_one(entry_id: int) -> Update:
    return update(AuditLog).where(AuditLog.id == entry_id).values(reason=AuditReason.CANCEL)


def _update_all(_: int) -> Update:
    return update(AuditLog).values(details={"changed": True})


def _delete_one(entry_id: int) -> Delete:
    return delete(AuditLog).where(AuditLog.id == entry_id)


def _delete_all(_: int) -> Delete:
    return delete(AuditLog)


@pytest.mark.parametrize("build", [_update_one, _update_all, _delete_one, _delete_all])
async def test_update_and_delete_are_rejected(
    db_session: AsyncSession, build: Callable[[int], Update | Delete]
) -> None:
    entry = await make_audit_log(db_session, details={"changed_fields": ["number"]})

    with pytest.raises(IntegrityError, match=APPEND_ONLY):
        async with db_session.begin_nested():
            await db_session.execute(build(entry.id))

    stored = await _fresh(db_session, entry.id)
    assert stored.reason is None
    assert stored.details == {"changed_fields": ["number"]}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET request_id = 'forged'",
        "DELETE FROM audit_log",
    ],
)
async def test_raw_sql_is_rejected(db_session: AsyncSession, statement: str) -> None:
    await make_audit_log(db_session)

    with pytest.raises(IntegrityError, match=APPEND_ONLY):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE audit_log",
        "TRUNCATE TABLE audit_log RESTART IDENTITY",
        "TRUNCATE events CASCADE",  # каскад по FK дотянулся бы до audit_log
    ],
)
async def test_truncate_is_rejected(db_session: AsyncSession, statement: str) -> None:
    entry = await make_audit_log(db_session)

    with pytest.raises(
        IntegrityError, match=r"audit_log доступна только для добавления \(TRUNCATE\)"
    ):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))

    assert (await _fresh(db_session, entry.id)).id == entry.id
    assert await _count(db_session, entry.event_id) == 1


async def test_event_delete_cascades_to_audit_log(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    other = await make_event(db_session)
    for _ in range(3):
        await make_audit_log(db_session, event_id=event.id)
    kept = await make_audit_log(db_session, event_id=other.id)

    await db_session.execute(delete(Event).where(Event.id == event.id))

    assert await _count(db_session, event.id) == 0
    assert await _count(db_session, other.id) == 1
    assert (await _fresh(db_session, kept.id)).event_id == other.id


async def test_user_delete_sets_actor_and_subject_to_null(db_session: AsyncSession) -> None:
    actor = await make_user(db_session)
    subject = await make_user(db_session)
    entry = await make_audit_log(
        db_session,
        action=AuditAction.TRANSFER_LEFT,
        reason=AuditReason.GO_SOLO,
        actor_kind=ActorKind.PARTICIPANT,
        actor_user_id=actor.id,
        subject_user_id=subject.id,
        details={"from_status": "assigned", "to_status": "solo"},
    )

    await db_session.execute(delete(User).where(User.id == actor.id))
    stored = await _fresh(db_session, entry.id)
    assert (stored.actor_user_id, stored.subject_user_id) == (None, subject.id)

    await db_session.execute(delete(User).where(User.id == subject.id))
    stored = await _fresh(db_session, entry.id)
    assert (stored.actor_user_id, stored.subject_user_id) == (None, None)
    # остальное в записи не тронуто
    assert stored.action == AuditAction.TRANSFER_LEFT
    assert stored.reason == AuditReason.GO_SOLO
    assert stored.actor_kind == ActorKind.PARTICIPANT
    assert stored.details == {"from_status": "assigned", "to_status": "solo"}


async def test_defaults_and_monotonic_id(db_session: AsyncSession) -> None:
    event = await make_event(db_session)

    first = await make_audit_log(db_session, event_id=event.id)
    second = await make_audit_log(db_session, event_id=event.id)

    stored = await _fresh(db_session, first.id)
    assert stored.details == {}
    assert stored.occurred_at is not None
    assert stored.reason is None
    assert (stored.actor_user_id, stored.subject_user_id) == (None, None)
    assert (stored.application_id, stored.transfer_id, stored.trip_id) == (None, None, None)
    assert (stored.job_id, stored.request_id) == (None, None)
    assert second.id > first.id


async def test_references_survive_without_foreign_keys(db_session: AsyncSession) -> None:
    """Заявка, трансфер и рейс могут быть удалены — запись о них остаётся."""
    ids = [uuid.uuid4() for _ in range(3)]
    entry = await make_audit_log(
        db_session, application_id=ids[0], transfer_id=ids[1], trip_id=ids[2]
    )

    stored = await _fresh(db_session, entry.id)
    assert [stored.application_id, stored.transfer_id, stored.trip_id] == ids


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("action", "ck_audit_log_audit_action"),
        ("reason", "ck_audit_log_audit_reason"),
        ("actor_kind", "ck_audit_log_actor_kind"),
    ],
)
async def test_unknown_enum_value_is_rejected(
    db_session: AsyncSession, column: str, constraint: str
) -> None:
    event = await make_event(db_session)
    values = {"action": "trip_updated", "reason": "cancel", "actor_kind": "system"}
    values[column] = "bogus"

    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO audit_log (event_id, action, reason, actor_kind)"
                    " VALUES (:event_id, :action, :reason, :actor_kind)"
                ),
                {"event_id": event.id, **values},
            )
