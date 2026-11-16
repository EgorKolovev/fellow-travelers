"""Защита от потока сообщений (M-04a): склейка, задержка, часовой лимит, гонка.

Сервисные тесты идут на `db_session` (одна внешняя транзакция: `now()` в ней не меняется, поэтому
сроки сравниваются точно). Гонка и связка с отправителем — на сессиях движка с commit.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    AuditAction,
    Event,
    Notification,
    NotificationKind,
    NotificationStatus,
    User,
)
from app.services import notification_store
from app.services.audit import SYSTEM, record
from app.services.notification_sender import SendResult, send_due
from app.services.notifications import notify, notify_many
from tests.factories import make_event, make_user
from tests.helpers.concurrency import wait
from tests.helpers.metrics import sample
from tests.services.test_notification_sender import FakeSender
from tests.services.test_notifications import pair, record_created, stored

K = NotificationKind
DEBOUNCE = timedelta(seconds=30)
HOUR = timedelta(hours=1)
HOURLY_LIMIT = 6


async def db_now(session: AsyncSession) -> datetime:
    value = await session.scalar(text("SELECT now()"))
    assert isinstance(value, datetime)
    return value


async def write(
    session: AsyncSession,
    user: User,
    group: Event,
    kind: NotificationKind = K.TRANSFER_CHANGED,
    *,
    key: str | None = None,
    payload: dict[str, str] | None = None,
) -> None:
    await notify(
        session,
        user_id=user.id,
        event_id=group.id,
        kind=kind,
        payload=payload or {"event_id": str(group.id)},
        dedupe_key=key or f"{kind.value}:{uuid.uuid4()}",
    )


async def add_history(
    session: AsyncSession,
    user: User,
    group: Event,
    *,
    status: NotificationStatus,
    moment: datetime | None,
) -> None:
    """Строка с готовым статусом; для `sent` `moment` — время отправки."""
    session.add(
        Notification(
            user_id=user.id,
            event_id=group.id,
            kind=K.TRANSFER_CHANGED,
            payload={"event_id": str(group.id)},
            dedupe_key=f"history:{uuid.uuid4()}",
            status=status,
            sent_at=moment,
        )
    )
    await session.flush()


async def person_and_group(session: AsyncSession) -> tuple[User, Event]:
    return await make_user(session), await make_event(session)


def planned(outcome: str) -> float:
    return sample("ft_notifications_planned_total", outcome=outcome)


# ---------------------------------------------------------------- склейка


async def test_same_kind_merges_into_one_row_with_new_payload(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    await write(db_session, person, group, K.FLIGHT_CHANGED, payload={"v": "1"}, key="a")
    (first,) = await stored(db_session, group.id)
    await db_session.execute(
        update(Notification).where(Notification.id == first.id).values(attempts=2)
    )

    await write(db_session, person, group, K.FLIGHT_CHANGED, payload={"v": "2"}, key="b")

    (row,) = await stored(db_session, group.id)
    assert row.id == first.id
    assert (row.kind, row.payload) == (K.FLIGHT_CHANGED, {"v": "2"})
    # срок, ключ и число попыток склейка не трогает
    assert (row.dedupe_key, row.attempts, row.run_after) == ("a", 2, first.run_after)


async def test_different_kinds_become_several_changes_with_event_id_only(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    await write(db_session, person, group, K.TRANSFER_ASSIGNED, key="a")
    (first,) = await stored(db_session, group.id)

    await write(db_session, person, group, K.FLIGHT_CHANGED, payload={"role": "mate"}, key="b")

    (row,) = await stored(db_session, group.id)
    assert row.id == first.id
    assert row.kind == K.SEVERAL_CHANGES
    assert row.payload == {"event_id": str(group.id)}
    assert (row.dedupe_key, row.run_after) == ("a", first.run_after)


async def test_several_changes_stays_several_changes(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    for kind in (K.TRANSFER_ASSIGNED, K.TRANSFER_LEFT, K.TRANSFER_LEFT, K.TRANSFER_ASSIGNED):
        await write(db_session, person, group, kind)

    (row,) = await stored(db_session, group.id)
    assert row.kind == K.SEVERAL_CHANGES


async def test_row_in_sending_is_not_merged_and_a_new_pending_appears(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    await write(db_session, person, group, K.TRANSFER_CHANGED)
    (first,) = await stored(db_session, group.id)
    await db_session.execute(
        update(Notification)
        .where(Notification.id == first.id)
        .values(status=NotificationStatus.SENDING)
    )

    await write(db_session, person, group, K.FLIGHT_CHANGED, payload={"role": "mate"})

    rows = await stored(db_session, group.id)
    assert [(r.status, r.kind) for r in rows] == [
        (NotificationStatus.SENDING, K.TRANSFER_CHANGED),
        (NotificationStatus.PENDING, K.FLIGHT_CHANGED),
    ]


async def test_sent_row_is_not_merged(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    await add_history(db_session, person, group, status=NotificationStatus.SENT, moment=None)

    await write(db_session, person, group)

    statuses = [r.status for r in await stored(db_session, group.id)]
    assert statuses == [NotificationStatus.SENT, NotificationStatus.PENDING]


async def test_merge_is_per_person_and_per_group(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    other_person = await make_user(db_session)
    other_group = await make_event(db_session)

    for user, grp in ((person, group), (other_person, group), (person, other_group)):
        await write(db_session, user, grp)
        await write(db_session, user, grp)

    assert len(await stored(db_session)) == 3


async def test_notify_many_merges_existing_and_inserts_the_rest_in_one_pass(
    db_session: AsyncSession,
) -> None:
    group = await make_event(db_session)
    old, new = await make_user(db_session), await make_user(db_session)
    await write(db_session, old, group, K.TRANSFER_ASSIGNED)

    await notify_many(
        db_session,
        user_ids=[old.id, new.id],
        event_id=group.id,
        kind=K.POINTS_CHANGED,
        payload={"event_id": str(group.id)},
        dedupe_key="points",
    )

    kinds = {r.user_id: r.kind for r in await stored(db_session, group.id)}
    assert kinds == {old.id: K.SEVERAL_CHANGES, new.id: K.POINTS_CHANGED}


async def test_repeat_of_a_sent_dedupe_key_is_ignored_silently(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    person, group = await person_and_group(db_session)
    await write(db_session, person, group, key="same")
    await db_session.execute(update(Notification).values(status=NotificationStatus.SENT))
    caplog.set_level(logging.WARNING)

    await write(db_session, person, group, key="same")

    assert [r.status for r in await stored(db_session, group.id)] == [NotificationStatus.SENT]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_planner_merges_history_of_two_transactions(db_session: AsyncSession) -> None:
    group, transfer, members = await pair(db_session)
    record_created(db_session, group, transfer, members)
    await db_session.commit()
    record(
        db_session,
        event_id=group.id,
        action=AuditAction.TRANSFER_JOINED,
        actor=SYSTEM,
        application=members[0],
        transfer_id=transfer.id,
    )
    await db_session.commit()

    rows = await stored(db_session, group.id)
    by_user = {r.user_id: r for r in rows}
    assert len(rows) == 2
    # members[0] получил `assigned` и `changed` (о нём самом запись joined не пишет ему),
    # members[1] — `assigned`, затем `changed` о вошедшем
    assert by_user[members[1].user_id].kind == K.SEVERAL_CHANGES
    assert by_user[members[0].user_id].kind in {K.TRANSFER_ASSIGNED, K.SEVERAL_CHANGES}


# ---------------------------------------------------------------- задержка


async def test_new_row_waits_for_the_debounce(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)

    await write(db_session, person, group)

    (row,) = await stored(db_session, group.id)
    assert row.run_after == await db_now(db_session) + DEBOUNCE


# ---------------------------------------------------------------- часовой лимит


async def fill_hour(
    session: AsyncSession, user: User, group: Event, count: int, *, status: NotificationStatus
) -> list[datetime]:
    """`count` строк за последний час: самая ранняя 50 минут назад, остальные позже."""
    now = await db_now(session)
    moments = [now - timedelta(minutes=50 - 5 * i) for i in range(count)]
    for moment in moments:
        await add_history(
            session,
            user,
            group,
            status=status,
            moment=moment if status == NotificationStatus.SENT else None,
        )
    return moments


async def test_sixth_message_in_the_hour_is_deferred_until_the_earliest_leaves(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    moments = await fill_hour(
        db_session, person, group, HOURLY_LIMIT, status=NotificationStatus.SENT
    )
    before = planned("deferred")

    await write(db_session, person, group)
    await db_session.commit()

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.run_after == moments[0] + HOUR
    assert planned("deferred") == before + 1


async def test_five_sent_messages_do_not_defer_the_next(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    await fill_hour(db_session, person, group, HOURLY_LIMIT - 1, status=NotificationStatus.SENT)
    before_created, before_deferred = planned("created"), planned("deferred")

    await write(db_session, person, group)
    await db_session.commit()

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.run_after == await db_now(db_session) + DEBOUNCE
    assert (planned("created"), planned("deferred")) == (before_created + 1, before_deferred)


async def test_rows_in_sending_count_towards_the_limit(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    moments = await fill_hour(
        db_session, person, group, HOURLY_LIMIT - 1, status=NotificationStatus.SENT
    )
    await add_history(db_session, person, group, status=NotificationStatus.SENDING, moment=None)

    await write(db_session, person, group)

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.run_after == moments[0] + HOUR


async def test_messages_older_than_an_hour_and_other_people_do_not_count(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    stranger, other_group = await make_user(db_session), await make_event(db_session)
    now = await db_now(db_session)
    for _ in range(HOURLY_LIMIT):
        await add_history(
            db_session,
            person,
            group,
            status=NotificationStatus.SENT,
            moment=now - HOUR - timedelta(seconds=1),
        )
        await add_history(db_session, stranger, group, status=NotificationStatus.SENT, moment=now)
        await add_history(
            db_session, person, other_group, status=NotificationStatus.SENT, moment=now
        )

    await write(db_session, person, group)

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.run_after == now + DEBOUNCE


async def test_more_than_the_limit_waits_for_enough_rows_to_leave(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    moments = await fill_hour(
        db_session, person, group, HOURLY_LIMIT + 2, status=NotificationStatus.SENT
    )

    await write(db_session, person, group)

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    # чтобы осталось 5, должны выйти три самых ранних: ждём выхода третьей
    assert row.run_after == moments[2] + HOUR


async def test_merging_into_a_deferred_row_keeps_its_deadline(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    moments = await fill_hour(
        db_session, person, group, HOURLY_LIMIT, status=NotificationStatus.SENT
    )
    await write(db_session, person, group, K.TRANSFER_CHANGED)
    merged_before = planned("merged")

    await write(db_session, person, group, K.FLIGHT_CHANGED, payload={"role": "mate"})
    await db_session.commit()

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.kind == K.SEVERAL_CHANGES
    assert row.run_after == moments[0] + HOUR
    assert planned("merged") == merged_before + 1


async def test_deadline_is_never_earlier_than_the_debounce(db_session: AsyncSession) -> None:
    person, group = await person_and_group(db_session)
    now = await db_now(db_session)
    # самая ранняя выходит из окна через 10 секунд, раньше задержки в 30 секунд
    for i in range(HOURLY_LIMIT):
        await add_history(
            db_session,
            person,
            group,
            status=NotificationStatus.SENT,
            moment=now - HOUR + timedelta(seconds=10 + i),
        )

    await write(db_session, person, group)

    (row,) = [
        r for r in await stored(db_session, group.id) if r.status == NotificationStatus.PENDING
    ]
    assert row.run_after == now + DEBOUNCE


# ---------------------------------------------------------------- метрика и запросы


async def test_metric_grows_only_after_commit_and_not_after_rollback(
    db_session: AsyncSession,
) -> None:
    person, group = await person_and_group(db_session)
    created, merged = planned("created"), planned("merged")

    await write(db_session, person, group)
    await write(db_session, person, group)
    assert (planned("created"), planned("merged")) == (created, merged)  # commit ещё не было
    await db_session.commit()

    assert (planned("created"), planned("merged")) == (created + 1, merged + 1)

    await write(db_session, await make_user(db_session), group)
    await db_session.rollback()
    assert planned("created") == created + 1


async def test_batch_costs_the_same_number_of_statements_for_any_size(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    group = await make_event(db_session)
    small = [(await make_user(db_session)).id for _ in range(2)]
    large = [(await make_user(db_session)).id for _ in range(25)]
    statements: list[str] = []

    def count(*args: object, **kwargs: object) -> None:
        statements.append("x")

    async def run(user_ids: list[uuid.UUID]) -> int:
        statements.clear()
        event.listen(db_engine.sync_engine, "before_cursor_execute", count)
        try:
            await notify_many(
                db_session,
                user_ids=user_ids,
                event_id=group.id,
                kind=K.POINTS_CHANGED,
                payload={"event_id": str(group.id)},
                dedupe_key=f"points:{uuid.uuid4()}",
            )
        finally:
            event.remove(db_engine.sync_engine, "before_cursor_execute", count)
        return len(statements)

    assert await run(large) == await run(small)


async def test_failure_to_store_warns_with_ids_and_does_not_raise(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    person, group = await person_and_group(db_session)
    calls: list[int] = []

    def never_inserts(*args: object, **kwargs: object) -> set[tuple[uuid.UUID, uuid.UUID]]:
        calls.append(1)
        return set()

    monkeypatch.setattr(notification_store, "_insert_new", never_inserts)
    caplog.set_level(logging.WARNING)

    await write(db_session, person, group)

    assert len(calls) == notification_store.MAX_ATTEMPTS
    assert await stored(db_session, group.id) == []
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    text_ = warning.getMessage()
    assert str(group.id) in text_
    assert str(person.id) in text_
    assert str(person.telegram_id) not in text_


# ---------------------------------------------------------------- гонка и отправитель


@dataclass
class Fixture:
    event_id: uuid.UUID
    user_ids: list[uuid.UUID]


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def committed(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Fixture]:
    async with sessions() as session:
        people = [await make_user(session) for _ in range(2)]
        group = await make_event(session)
        await session.commit()
        created = Fixture(event_id=group.id, user_ids=[p.id for p in people])
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(delete(User).where(User.id.in_(created.user_ids)))
            await session.commit()


async def _notify_in_own_transaction(
    sessions: async_sessionmaker[AsyncSession],
    committed: Fixture,
    kind: NotificationKind,
    ready: asyncio.Barrier,
) -> None:
    async with sessions() as session:
        await ready.wait()
        await notify(
            session,
            user_id=committed.user_ids[0],
            event_id=committed.event_id,
            kind=kind,
            payload={"event_id": str(committed.event_id)},
            dedupe_key=f"{kind.value}:{uuid.uuid4()}",
        )
        await asyncio.sleep(0.05)  # вторая транзакция успевает упереться в чужую вставку
        await session.commit()


async def test_two_parallel_transactions_leave_one_pending_row(
    sessions: async_sessionmaker[AsyncSession], committed: Fixture
) -> None:
    for _ in range(5):  # одна проба гонку не гарантирует
        async with sessions() as cleaner:
            await cleaner.execute(
                delete(Notification).where(Notification.event_id == committed.event_id)
            )
            await cleaner.commit()
        ready = asyncio.Barrier(2)

        await wait(
            asyncio.gather(
                _notify_in_own_transaction(sessions, committed, K.TRANSFER_CHANGED, ready),
                _notify_in_own_transaction(sessions, committed, K.FLIGHT_CHANGED, ready),
            )
        )

        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(Notification).where(Notification.event_id == committed.event_id)
                )
            ).all()
        assert [(r.status, r.kind) for r in rows] == [
            (NotificationStatus.PENDING, K.SEVERAL_CHANGES)
        ]


async def test_new_row_is_taken_only_after_the_debounce(
    sessions: async_sessionmaker[AsyncSession], committed: Fixture
) -> None:
    async with sessions() as session:
        await notify(
            session,
            user_id=committed.user_ids[0],
            event_id=committed.event_id,
            kind=K.POINTS_CHANGED,
            payload={"event_id": str(committed.event_id)},
            dedupe_key="points:1",
        )
        await session.commit()
    now = datetime.now(UTC)
    sender = FakeSender()

    assert await send_due(sessions, sender, now=now) == SendResult()
    assert await send_due(sessions, sender, now=now + timedelta(seconds=31)) == SendResult(sent=1)


async def test_event_before_the_send_is_merged_but_event_after_it_is_a_new_row(
    sessions: async_sessionmaker[AsyncSession], committed: Fixture
) -> None:
    async def emit(kind: NotificationKind) -> None:
        async with sessions() as session:
            await notify(
                session,
                user_id=committed.user_ids[0],
                event_id=committed.event_id,
                kind=kind,
                payload={"event_id": str(committed.event_id)},
                dedupe_key=f"{kind.value}:{uuid.uuid4()}",
            )
            await session.commit()

    await emit(K.TRANSFER_CHANGED)
    await emit(K.TRANSFER_CHANGED)
    now = datetime.now(UTC) + timedelta(seconds=31)
    assert await send_due(sessions, FakeSender(), now=now) == SendResult(sent=1)

    await emit(K.TRANSFER_CHANGED)  # первая строка уже отправлена: нужна новая

    async with sessions() as session:
        counts = dict(
            (
                await session.execute(
                    select(Notification.status, func.count())
                    .where(Notification.event_id == committed.event_id)
                    .group_by(Notification.status)
                )
            ).all()
        )
    assert counts == {NotificationStatus.SENT: 1, NotificationStatus.PENDING: 1}
