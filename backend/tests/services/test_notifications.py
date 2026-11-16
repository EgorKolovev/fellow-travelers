"""Механизм уведомлений: запись истории -> строка `pending` после commit (M-04).

Планировщик срабатывает на `before_commit` любой сессии: проверяем сессию теста, вложенную
транзакцию, откат, сессию воркера и прямой `notify`.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditReason,
    Event,
    Notification,
    NotificationKind,
    NotificationStatus,
    Transfer,
)
from app.services.audit import SYSTEM, Actor, record
from app.services.notifications import notify
from tests.factories import make_application, make_event, make_transfer, make_user
from tests.helpers.audit_replay import audit_rows
from tests.helpers.jobs import shared_session_factory


async def pair(session: AsyncSession) -> tuple[Event, Transfer, list[Application]]:
    """Группа с трансфером из двух заявок; истории нет — её пишет тест."""
    event = await make_event(session)
    transfer = await make_transfer(session, event_id=event.id)
    members = [
        await make_application(session, event_id=event.id, transfer_id=transfer.id)
        for _ in range(2)
    ]
    return event, transfer, members


def record_created(
    session: AsyncSession, event: Event, transfer: Transfer, members: Sequence[Application]
) -> None:
    record(
        session,
        event_id=event.id,
        action=AuditAction.TRANSFER_CREATED,
        actor=SYSTEM,
        reason=AuditReason.AUTO_MATCH,
        transfer_id=transfer.id,
        details={"application_ids": [str(m.id) for m in members]},
    )


async def stored(session: AsyncSession, event_id: uuid.UUID | None = None) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.id).execution_options(populate_existing=True)
    if event_id is not None:
        stmt = stmt.where(Notification.event_id == event_id)
    return list((await session.scalars(stmt)).all())


async def test_history_record_becomes_pending_notification_after_commit(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    record_created(db_session, event, transfer, members)
    assert await stored(db_session, event.id) == []  # до commit планировщик ещё не работал

    await db_session.commit()

    rows = await stored(db_session, event.id)
    assert {row.user_id for row in rows} == {m.user_id for m in members}
    (audit,) = await audit_rows(db_session, event.id)
    for row in rows:
        assert row.kind == NotificationKind.TRANSFER_ASSIGNED
        assert row.status == NotificationStatus.PENDING
        assert (row.attempts, row.sent_at, row.last_error) == (0, None, None)
        assert row.dedupe_key == f"transfer_assigned:{audit.id}"
        assert row.payload == {"event_id": str(event.id), "transfer_id": str(transfer.id)}


async def test_rollback_leaves_no_notifications_and_forgets_pending(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    await db_session.commit()
    event_id = event.id
    record_created(db_session, event, transfer, members)
    await db_session.flush()

    await db_session.rollback()
    await db_session.commit()  # следующая транзакция той же сессии не вспоминает откатанное

    assert await stored(db_session, event_id) == []
    assert await audit_rows(db_session, event_id) == []


async def test_transaction_without_history_makes_no_notifications(
    db_session: AsyncSession,
) -> None:
    await pair(db_session)

    await db_session.commit()

    assert await stored(db_session) == []


async def test_nested_commit_waits_for_outer_commit(db_session: AsyncSession) -> None:
    event, transfer, members = await pair(db_session)
    async with db_session.begin_nested():
        record_created(db_session, event, transfer, members)
    assert await stored(db_session, event.id) == []  # savepoint закрыт, сценарий ещё нет
    record(
        db_session,
        event_id=event.id,
        action=AuditAction.TRANSFER_JOINED,
        actor=SYSTEM,
        application=members[0],
        transfer_id=transfer.id,
    )

    await db_session.commit()

    rows = await stored(db_session, event.id)
    # записи одной транзакции дают одно сообщение на человека
    assert len(rows) == 2
    assert len({row.user_id for row in rows}) == 2


async def test_scheduler_runs_in_worker_sessions(db_session: AsyncSession) -> None:
    event, transfer, members = await pair(db_session)
    factory = shared_session_factory(db_session)
    async with factory() as worker_session:
        record_created(worker_session, event, transfer, members)
        await worker_session.commit()

    assert len(await stored(db_session, event.id)) == 2


async def test_kinds_without_message_by_themselves(db_session: AsyncSession) -> None:
    event, transfer, members = await pair(db_session)
    for action in (
        AuditAction.TRANSFER_DEPARTED,
        AuditAction.MARKS_CHANGED,
        AuditAction.APPLICATION_SUBMITTED,
        AuditAction.APPLICATION_UPDATED,
        AuditAction.STATUS_CHANGED,
        AuditAction.TRANSFER_DISSOLVED,
    ):
        record(
            db_session,
            event_id=event.id,
            action=action,
            actor=SYSTEM,
            application=members[0],
            transfer_id=transfer.id,
        )

    await db_session.commit()

    assert await stored(db_session) == []


async def test_own_action_is_not_reported_to_actor(db_session: AsyncSession) -> None:
    event, transfer, members = await pair(db_session)
    record(
        db_session,
        event_id=event.id,
        action=AuditAction.TRANSFER_CREATED,
        actor=Actor(ActorKind.PARTICIPANT, members[0].user_id),
        transfer_id=transfer.id,
        details={"application_ids": [str(m.id) for m in members]},
    )

    await db_session.commit()

    (row,) = await stored(db_session, event.id)
    assert row.user_id == members[1].user_id


async def test_notify_ignores_repeated_key(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    person = await make_user(db_session)
    key = f"points_changed:{event.id}:moment"

    for _ in range(2):
        await notify(
            db_session,
            user_id=person.id,
            event_id=event.id,
            kind=NotificationKind.POINTS_CHANGED,
            payload={"event_id": str(event.id)},
            dedupe_key=key,
        )
    await notify(
        db_session,
        user_id=person.id,
        event_id=event.id,
        kind=NotificationKind.POINTS_CHANGED,
        payload={"event_id": str(event.id)},
        dedupe_key=f"{key}-later",
    )
    await db_session.commit()

    assert [row.dedupe_key for row in await stored(db_session, event.id)] == [key, f"{key}-later"]


async def test_planner_ignores_rolled_back_savepoint(db_session: AsyncSession) -> None:
    event, transfer, members = await pair(db_session)
    try:
        async with db_session.begin_nested():
            record_created(db_session, event, transfer, members)
            raise RuntimeError("сценарий отказал внутри savepoint")
    except RuntimeError:
        pass

    await db_session.commit()

    assert await stored(db_session) == []
    count = await db_session.scalar(select(func.count()).select_from(Notification))
    assert count == 0


async def test_savepoint_rollback_keeps_history_of_outer_transaction(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    record_created(db_session, event, transfer, members)  # внешняя транзакция
    try:
        async with db_session.begin_nested():
            raise RuntimeError("обработанная ошибка внутри savepoint")
    except RuntimeError:
        pass

    await db_session.commit()

    rows = await stored(db_session, event.id)
    assert {row.user_id for row in rows} == {m.user_id for m in members}


async def _leave(
    session: AsyncSession,
    event: Event,
    transfer: Transfer,
    leaver: Application,
    reason: AuditReason,
) -> None:
    leaver.transfer_id = None
    leaver.status = ApplicationStatus.SEARCHING
    await session.flush()
    record(
        session,
        event_id=event.id,
        action=AuditAction.TRANSFER_LEFT,
        actor=SYSTEM,
        reason=reason,
        application=leaver,
        transfer_id=transfer.id,
    )


async def test_leaving_because_of_cancelled_flight_does_not_message_the_leaver(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    third = await make_application(db_session, event_id=event.id, transfer_id=transfer.id)
    await _leave(db_session, event, transfer, third, AuditReason.FLIGHT_CANCELLED)

    await db_session.commit()

    kinds = {row.user_id: row.kind for row in await stored(db_session, event.id)}
    assert kinds == {m.user_id: NotificationKind.TRANSFER_CHANGED for m in members}


async def test_leaving_for_ordinary_reason_still_messages_the_leaver(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    third = await make_application(db_session, event_id=event.id, transfer_id=transfer.id)
    await _leave(db_session, event, transfer, third, AuditReason.INCOMPATIBLE_AFTER_CHANGE)

    await db_session.commit()

    kinds = {row.user_id: row.kind for row in await stored(db_session, event.id)}
    assert kinds[third.user_id] == NotificationKind.TRANSFER_LEFT
    assert {kinds[m.user_id] for m in members} == {NotificationKind.TRANSFER_CHANGED}


async def test_departed_member_is_not_notified_about_transfer_changes(
    db_session: AsyncSession,
) -> None:
    event, transfer, members = await pair(db_session)
    departed = await make_application(
        db_session, event_id=event.id, transfer_id=transfer.id, departed_at=datetime.now(UTC)
    )
    await _leave(db_session, event, transfer, members[1], AuditReason.MEMBER_LEFT)

    await db_session.commit()

    users = {row.user_id for row in await stored(db_session, event.id)}
    assert departed.user_id not in users
    assert users == {members[0].user_id, members[1].user_id}
