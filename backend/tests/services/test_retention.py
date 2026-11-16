"""Удаление данных группы через 7 суток (Д11): срок, каскад, устойчивость к ошибкам."""

import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Application,
    Base,
    Event,
    Notification,
    NotificationKind,
    User,
)
from app.services import retention
from app.services.retention import expired_event_ids, purge_event, purge_expired
from tests.factories import (
    make_application,
    make_audit_log,
    make_job,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)
from tests.factories import (
    make_event as factory_make_event,
)

ENDS_ON = date(2026, 11, 14)
# конец дня 14.11 по UTC−12 = 15.11 12:00 UTC; плюс 7 суток
ENDS_DEADLINE = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
UTC_PLUS_7 = timezone(timedelta(hours=7))


LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def stamp_event(
    session: AsyncSession,
    event: Event,
    *,
    created_at: datetime = LONG_AGO,
    updated_at: datetime = LONG_AGO,
) -> None:
    """Ставит created_at/updated_at группы SQL-ом (в одном UPDATE: onupdate не перезапишет)."""
    await session.execute(
        update(Event)
        .where(Event.id == event.id)
        .values(created_at=created_at, updated_at=updated_at)
    )
    await session.refresh(event)


async def make_event(session: AsyncSession, **kw: Any) -> Event:
    """Группа «давно созданная и давно правленная»: срок зависит только от ends_on и «Уехал»."""
    event = await factory_make_event(session, **kw)
    await stamp_event(session, event)
    return event


def factory_of(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх соединения теста: каждая сессия — savepoint во внешней транзакции."""
    return async_sessionmaker(
        bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


async def make_departed(
    session: AsyncSession, event: Event, departed_at: datetime | None
) -> Application:
    return await make_application(session, event_id=event.id, departed_at=departed_at)


async def ended(session: AsyncSession, ends_on: date, **kw: str) -> Event:
    return await make_event(session, starts_on=ends_on, ends_on=ends_on, **kw)


async def exists(session: AsyncSession, event_id: uuid.UUID) -> bool:
    return (
        await session.scalar(select(func.count()).select_from(Event).where(Event.id == event_id))
    ) == 1


# ---------------------------------------------------------------- срок


@pytest.mark.parametrize(
    ("now", "expired"),
    [
        (ENDS_DEADLINE - SECOND, False),  # на секунду раньше границы
        (ENDS_DEADLINE, True),  # ровно на границе: условие >=
        (ENDS_DEADLINE + SECOND, True),
        # 22.11 00:00 UTC: в UTC группа «уже неделю как» кончилась, но в UTC−12 ещё нет
        (datetime(2026, 11, 22, 0, 0, tzinfo=UTC), False),
        # тот же момент в другой записи пояса сравнивается как момент времени
        (datetime(2026, 11, 22, 19, 0, tzinfo=UTC_PLUS_7), True),
        (datetime(2026, 11, 22, 18, 59, tzinfo=UTC_PLUS_7), False),
    ],
)
async def test_deadline_is_end_of_ends_on_in_utc_minus_12_plus_7_days(
    db_session: AsyncSession, now: datetime, expired: bool
) -> None:
    event = await make_event(db_session, starts_on=ENDS_ON, ends_on=ENDS_ON)

    assert (event.id in await expired_event_ids(db_session, now)) is expired


async def test_group_ended_8_days_ago_without_marks_expires(db_session: AsyncSession) -> None:
    # 8 суток: 12.11 + 1 день + 12 ч + 7 суток = 20.11 12:00 UTC, до того группа ещё хранится
    now = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
    old = await ended(db_session, (now - timedelta(days=8)).date())
    fresh = await ended(db_session, (now - timedelta(days=6)).date())

    ids = await expired_event_ids(db_session, now)

    assert old.id in ids
    assert fresh.id not in ids


async def test_late_departure_mark_extends_the_deadline(db_session: AsyncSession) -> None:
    event = await make_event(db_session, starts_on=ENDS_ON, ends_on=ENDS_ON)
    departed = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)  # через 2 дня после конца дня ends_on
    await make_departed(db_session, event, departed)
    await make_departed(db_session, event, departed - timedelta(days=1))  # ранняя не важна
    await make_departed(db_session, event, None)  # без отметки не важна
    deadline = departed + retention.RETENTION_PERIOD

    assert event.id not in await expired_event_ids(db_session, ENDS_DEADLINE)
    assert event.id not in await expired_event_ids(db_session, deadline - SECOND)
    assert event.id in await expired_event_ids(db_session, deadline)


async def test_early_departure_mark_does_not_shorten_the_deadline(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session, starts_on=ENDS_ON, ends_on=ENDS_ON)
    await make_departed(db_session, event, datetime(2026, 11, 13, 9, 0, tzinfo=UTC))

    assert event.id not in await expired_event_ids(db_session, ENDS_DEADLINE - SECOND)
    assert event.id in await expired_event_ids(db_session, ENDS_DEADLINE)


async def test_expired_ids_are_ordered_by_deadline_and_limited(db_session: AsyncSession) -> None:
    now = datetime(2026, 12, 31, 0, 0, tzinfo=UTC)
    newer = await make_event(db_session, starts_on=date(2026, 11, 10), ends_on=date(2026, 11, 12))
    oldest = await make_event(db_session, starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 3))
    middle = await make_event(db_session, starts_on=date(2026, 11, 5), ends_on=date(2026, 11, 7))
    # «Уехал» сдвигает группу в конец очереди
    latest = await make_event(db_session, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 2))
    await make_departed(db_session, latest, datetime(2026, 12, 1, tzinfo=UTC))

    assert await expired_event_ids(db_session, now) == [
        oldest.id,
        middle.id,
        newer.id,
        latest.id,
    ]
    assert await expired_event_ids(db_session, now, limit=2) == [oldest.id, middle.id]


async def test_backdated_ends_on_of_fresh_group_keeps_seven_days_from_creation(
    db_session: AsyncSession,
) -> None:
    created = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    event = await factory_make_event(
        db_session, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 18)
    )
    await stamp_event(db_session, event, created_at=created, updated_at=created)
    deadline = created + retention.RETENTION_PERIOD

    assert event.id not in await expired_event_ids(db_session, created + timedelta(hours=1))
    assert event.id not in await expired_event_ids(db_session, deadline - SECOND)
    assert event.id in await expired_event_ids(db_session, deadline)


async def test_backdated_ends_on_set_by_edit_keeps_seven_days_from_update(
    db_session: AsyncSession,
) -> None:
    updated = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    event = await make_event(db_session, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 18))
    await stamp_event(db_session, event, updated_at=updated)
    deadline = updated + retention.RETENTION_PERIOD

    assert event.id not in await expired_event_ids(db_session, deadline - SECOND)
    assert event.id in await expired_event_ids(db_session, deadline)


async def test_ordinary_edit_of_group_bumps_updated_at_and_moves_the_deadline(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 2))
    now = datetime.now(UTC)
    assert event.id in await expired_event_ids(db_session, now)

    await db_session.execute(update(Event).where(Event.id == event.id).values(title="Новое"))

    assert event.id not in await expired_event_ids(db_session, now + timedelta(days=6))
    assert event.id in await expired_event_ids(db_session, now + timedelta(days=8))


# ---------------------------------------------------------------- каскад


async def count_rows(session: AsyncSession, table: str, event_id: uuid.UUID) -> int:
    column = "id" if table == "events" else "event_id"
    result = await session.scalar(
        text(f'SELECT count(*) FROM "{table}" WHERE {column} = :id'), {"id": event_id}
    )
    return int(result or 0)


def tables_of_event() -> list[str]:
    """Все таблицы, у которых есть колонка event_id, и сама events: новая таблица не потеряется."""
    names = [t.name for t in Base.metadata.sorted_tables if "event_id" in t.columns]
    return ["events", *names]


async def fill_event(session: AsyncSession, event: Event) -> User:
    """Заполняет группу строками во всех таблицах с event_id; возвращает участника."""
    member = await make_user(session)
    await make_membership(session, event_id=event.id, user_id=member.id)
    transfer = await make_transfer(session, event_id=event.id)
    trip = await make_trip(session, event_id=event.id)
    await make_application(
        session,
        event_id=event.id,
        user_id=member.id,
        trip_id=trip.id,
        transfer_id=transfer.id,
        departed_at=datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
    )
    await make_audit_log(session, event_id=event.id, transfer_id=transfer.id)
    await make_job(session, event_id=event.id)
    session.add(
        Notification(
            user_id=member.id,
            event_id=event.id,
            kind=NotificationKind.TRANSFER_ASSIGNED,
            payload={},
            dedupe_key="k1",
        )
    )
    await session.flush()
    return member


async def test_purge_cascades_to_every_table_with_event_id(db_session: AsyncSession) -> None:
    doomed = await make_event(db_session)
    kept = await make_event(db_session)
    member = await fill_event(db_session, doomed)
    kept_member = await fill_event(db_session, kept)
    tables = tables_of_event()
    for table in tables:
        assert await count_rows(db_session, table, doomed.id) > 0, f"заполните {table} в fill_event"
    kept_before = {t: await count_rows(db_session, t, kept.id) for t in tables}

    assert await purge_event(db_session, doomed.id, now=datetime(2027, 1, 1, tzinfo=UTC))

    for table in tables:
        assert await count_rows(db_session, table, doomed.id) == 0, table
    assert {t: await count_rows(db_session, t, kept.id) for t in tables} == kept_before
    assert await db_session.get(User, member.id) is not None
    assert await db_session.get(User, kept_member.id) is not None
    admin_phone = await db_session.scalar(select(User.phone).where(User.id == member.id))
    assert admin_phone is None or isinstance(admin_phone, str)


async def test_purge_event_deletes_users_of_group_not_at_all(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    member = await make_user(db_session, phone="+79991112233")
    await make_membership(db_session, event_id=event.id, user_id=member.id)

    await purge_event(db_session, event.id, now=datetime(2027, 1, 1, tzinfo=UTC))

    assert await db_session.scalar(select(User.phone).where(User.id == member.id)) == "+79991112233"
    assert await db_session.scalar(select(User.id).where(User.id == event.admin_id)) is not None


async def test_purge_event_keeps_group_that_was_extended_after_selection(
    db_session: AsyncSession,
) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    event = await make_event(db_session, starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 3))
    assert event.id in await expired_event_ids(db_session, now)

    await db_session.execute(
        update(Event).where(Event.id == event.id).values(ends_on=date(2027, 1, 1))
    )
    assert await purge_event(db_session, event.id, now=now) is False
    assert await exists(db_session, event.id)


async def test_purge_event_keeps_group_whose_ends_on_was_moved_to_the_past(
    db_session: AsyncSession,
) -> None:
    moment = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    event = await make_event(db_session, starts_on=date(2026, 10, 1), ends_on=date(2026, 12, 1))
    await db_session.execute(
        update(Event)
        .where(Event.id == event.id)
        .values(ends_on=date(2026, 10, 18), updated_at=moment)
    )

    assert await purge_event(db_session, event.id, now=moment + timedelta(hours=1)) is False
    assert await exists(db_session, event.id)
    assert await purge_event(db_session, event.id, now=moment + timedelta(days=7)) is True


async def test_purge_event_keeps_group_with_new_departure_mark(db_session: AsyncSession) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    event = await make_event(db_session, starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 3))
    application = await make_departed(db_session, event, None)
    assert event.id in await expired_event_ids(db_session, now)

    await db_session.execute(
        update(Application)
        .where(Application.id == application.id)
        .values(departed_at=now - timedelta(days=1))
    )

    assert await purge_event(db_session, event.id, now=now) is False
    assert await exists(db_session, event.id)


async def test_purge_event_unknown_id_returns_false(db_session: AsyncSession) -> None:
    assert (
        await purge_event(db_session, uuid.uuid4(), now=datetime(2027, 1, 1, tzinfo=UTC)) is False
    )


# ---------------------------------------------------------------- проход


async def test_purge_expired_deletes_only_expired_and_logs_count(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    old1 = await ended(db_session, date(2026, 11, 1), title="Секретное имя")
    old2 = await ended(db_session, date(2026, 11, 2))
    fresh = await ended(db_session, date(2026, 11, 28))
    caplog.set_level(logging.INFO, logger=retention.logger.name)

    purged = await purge_expired(factory_of(db_session), now=now)

    assert purged == 2
    assert not await exists(db_session, old1.id)
    assert not await exists(db_session, old2.id)
    assert await exists(db_session, fresh.id)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert [r.getMessage() for r in infos] == ["Удалено групп: 2"]
    assert "Секретное имя" not in caplog.text


async def test_purge_expired_respects_limit(db_session: AsyncSession) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    events = [await ended(db_session, date(2026, 11, d)) for d in (1, 2, 3)]

    assert await purge_expired(factory_of(db_session), now=now, limit=2) == 2
    assert await purge_expired(factory_of(db_session), now=now, limit=2) == 1
    assert [await exists(db_session, e.id) for e in events] == [False, False, False]


async def test_error_in_one_group_does_not_stop_the_others(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    broken = await ended(db_session, date(2026, 11, 1))
    fine = await ended(db_session, date(2026, 11, 2))
    original = retention.purge_event

    async def flaky(session: AsyncSession, event_id: uuid.UUID, **kw: datetime | None) -> bool:
        if event_id == broken.id:
            raise ConnectionResetError("secret detail")
        return await original(session, event_id, **kw)

    monkeypatch.setattr(retention, "purge_event", flaky)
    caplog.set_level(logging.INFO, logger=retention.logger.name)

    assert await purge_expired(factory_of(db_session), now=now) == 1

    assert await exists(db_session, broken.id)
    assert not await exists(db_session, fine.id)
    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert str(broken.id) in error.getMessage()
    assert "ConnectionResetError" in error.getMessage()
    assert "secret detail" not in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        "Удалено групп: 1"
    ]
