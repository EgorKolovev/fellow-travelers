"""Таблица notifications: значения по умолчанию, CHECK, уникальность и каскады."""

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, Notification, NotificationKind, NotificationStatus, User
from tests.factories import make_event, make_user


async def make_notification(
    session: AsyncSession, event: Event, user: User, **kw: object
) -> Notification:
    kw.setdefault("kind", NotificationKind.POINTS_CHANGED)
    kw.setdefault("payload", {"event_id": str(event.id)})
    kw.setdefault("dedupe_key", "points_changed:1")
    notification = Notification(event_id=event.id, user_id=user.id, **kw)
    session.add(notification)
    await session.flush()
    return notification


async def test_defaults(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    person = await make_user(db_session)

    created = await make_notification(db_session, event, person)
    stmt = (
        select(Notification)
        .where(Notification.id == created.id)
        .execution_options(populate_existing=True)
    )
    row = (await db_session.execute(stmt)).scalar_one()

    assert row.status == NotificationStatus.PENDING
    assert row.attempts == 0
    assert (row.sent_at, row.last_error) == (None, None)
    now = await db_session.scalar(text("SELECT now()"))
    assert row.run_after == row.created_at == now


async def test_dedupe_key_is_unique_per_user(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first = await make_user(db_session)
    second = await make_user(db_session)
    await make_notification(db_session, event, first)
    await make_notification(db_session, event, second)  # тот же ключ другому человеку можно

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_notification(db_session, event, first)


@pytest.mark.parametrize(
    ("kind", "status"), [("unknown", "pending"), ("points_changed", "unknown")]
)
async def test_unknown_kind_and_status_are_rejected(
    db_session: AsyncSession, kind: str, status: str
) -> None:
    event = await make_event(db_session)
    person = await make_user(db_session)
    insert = text(
        "INSERT INTO notifications (user_id, event_id, kind, payload, dedupe_key, status)"
        " VALUES (:user_id, :event_id, :kind, '{}', 'k', :status)"
    ).bindparams(user_id=person.id, event_id=event.id, kind=kind, status=status)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(insert)


@pytest.mark.parametrize("owner", ["event", "user"])
async def test_rows_go_with_event_or_user(db_session: AsyncSession, owner: str) -> None:
    event = await make_event(db_session)
    person = await make_user(db_session)
    await make_notification(db_session, event, person)
    await db_session.commit()

    target = event if owner == "event" else person
    await db_session.execute(delete(type(target)).where(type(target).id == target.id))

    count = await db_session.scalar(select(func.count()).select_from(Notification))
    assert count == 0
