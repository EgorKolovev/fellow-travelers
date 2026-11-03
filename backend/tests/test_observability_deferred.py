"""Отложенные до commit действия: когда они применяются и когда пропадают.

Счётчики здесь свои, в отдельном реестре, а транзакции — настоящие (`db_session` на Postgres),
потому что порядок событий commit и rollback у savepoint важен.
"""

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.observability.deferred import defer, defer_increment, defer_observe


class FailureError(Exception):
    pass


@pytest.fixture
def counter() -> Counter:
    return Counter("deferred_events", "тест", ["name"], registry=CollectorRegistry())


def value(counter: Counter, name: str) -> float:
    return float(counter.labels(name)._value.get())


async def fail_in_savepoint(session: AsyncSession, counter: Counter, name: str) -> None:
    """Откладывает действие внутри savepoint, который затем откатывается."""
    async with session.begin_nested():
        defer_increment(session, counter.labels(name))
        raise FailureError


async def fail_with_inner_release(session: AsyncSession, counter: Counter) -> None:
    """Внешний savepoint откатывается после того, как внутренний уже отпущен."""
    async with session.begin_nested():
        defer_increment(session, counter.labels("outer_sp"))
        async with session.begin_nested():
            defer_increment(session, counter.labels("inner_sp"))
        raise FailureError


async def touch(session: AsyncSession) -> None:
    """Открывает транзакцию, как это делает любой запрос."""
    await session.execute(text("SELECT 1"))


async def test_applied_after_commit_not_before(db_session: AsyncSession, counter: Counter) -> None:
    await touch(db_session)

    defer_increment(db_session, counter.labels("a"))

    assert value(counter, "a") == 0
    await db_session.commit()
    assert value(counter, "a") == 1


async def test_applied_once(db_session: AsyncSession, counter: Counter) -> None:
    await touch(db_session)
    defer_increment(db_session, counter.labels("a"))
    await db_session.commit()

    await touch(db_session)
    await db_session.commit()

    assert value(counter, "a") == 1


async def test_dropped_on_rollback(db_session: AsyncSession, counter: Counter) -> None:
    await touch(db_session)
    defer_increment(db_session, counter.labels("a"))

    await db_session.rollback()
    await touch(db_session)
    await db_session.commit()

    assert value(counter, "a") == 0


async def test_dropped_when_session_is_closed(db_session: AsyncSession, counter: Counter) -> None:
    await touch(db_session)
    defer_increment(db_session, counter.labels("a"))

    await db_session.close()
    await touch(db_session)
    await db_session.commit()

    assert value(counter, "a") == 0


async def test_released_savepoint_waits_for_root_commit(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    async with db_session.begin_nested():
        defer_increment(db_session, counter.labels("inner"))

    assert value(counter, "inner") == 0  # savepoint отпущен, но транзакция ещё не закоммичена
    await db_session.commit()
    assert value(counter, "inner") == 1


async def test_rolled_back_savepoint_drops_only_its_own_actions(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    defer_increment(db_session, counter.labels("outer"))
    async with db_session.begin_nested():
        defer_increment(db_session, counter.labels("kept"))
    with pytest.raises(FailureError):
        await fail_in_savepoint(db_session, counter, "lost")

    await db_session.commit()

    assert value(counter, "outer") == 1
    assert value(counter, "kept") == 1
    assert value(counter, "lost") == 0


async def test_released_inner_savepoint_is_dropped_with_rolled_back_outer(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    with pytest.raises(FailureError):
        await fail_with_inner_release(db_session, counter)

    await db_session.commit()

    assert value(counter, "outer_sp") == 0
    assert value(counter, "inner_sp") == 0


async def test_nested_savepoints_released_all_apply_on_root_commit(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    async with db_session.begin_nested():
        defer_increment(db_session, counter.labels("outer_sp"))
        async with db_session.begin_nested():
            defer_increment(db_session, counter.labels("inner_sp"))

    await db_session.commit()

    assert value(counter, "outer_sp") == 1
    assert value(counter, "inner_sp") == 1


async def test_rollback_after_savepoint_release_drops_everything(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    async with db_session.begin_nested():
        defer_increment(db_session, counter.labels("inner"))

    await db_session.rollback()
    await touch(db_session)
    await db_session.commit()

    assert value(counter, "inner") == 0


async def test_actions_of_one_transaction_do_not_leak_into_the_next(
    db_session: AsyncSession, counter: Counter
) -> None:
    await touch(db_session)
    defer_increment(db_session, counter.labels("first"))
    await db_session.rollback()

    await touch(db_session)
    defer_increment(db_session, counter.labels("second"))
    await db_session.commit()

    assert value(counter, "first") == 0
    assert value(counter, "second") == 1


async def test_observe_is_deferred_too(db_session: AsyncSession) -> None:
    histogram = Histogram("deferred_seconds", "тест", registry=CollectorRegistry())
    await touch(db_session)

    defer_observe(db_session, histogram, 0.5)

    assert histogram._sum.get() == 0
    await db_session.commit()
    assert histogram._sum.get() == 0.5


async def test_failing_action_does_not_break_commit(
    db_session: AsyncSession, counter: Counter, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> None:
        raise FailureError

    await touch(db_session)
    defer(db_session, broken)
    defer_increment(db_session, counter.labels("after"))

    await db_session.commit()

    assert value(counter, "after") == 1
    assert any("Отложенное действие не выполнено" in r.getMessage() for r in caplog.records)


def test_without_transaction_action_runs_at_once(counter: Counter) -> None:
    session = Session()

    defer_increment(session, counter.labels("now"))

    assert value(counter, "now") == 1
