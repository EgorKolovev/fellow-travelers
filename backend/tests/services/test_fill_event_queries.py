"""Число SQL-запросов `fill_event` не зависит от числа ждущих заявок.

Состояние группы читается один раз, а решения обновляют его в памяти: иначе каждая ждущая заявка
стоила бы нескольких запросов под блокировкой группы.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Application, ApplicationStatus
from app.observability.metrics import MatchTrigger
from app.services.matching import FillResult, fill_event, match_application
from tests.factories import make_application, make_event, make_trip

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)


async def _app(
    session: AsyncSession, event_id: uuid.UUID, minutes: int = 0, **kw: Any
) -> Application:
    trip = await make_trip(
        session, event_id=event_id, scheduled_arrival=BASE + timedelta(minutes=minutes)
    )
    kw.setdefault("baggage_count", 0)
    return await make_application(session, event_id=event_id, trip_id=trip.id, **kw)


async def count_statements(engine: AsyncEngine, session: AsyncSession, event_id: uuid.UUID) -> int:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    await session.flush()
    sa_event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        result = await fill_event(session, event_id, trigger=MatchTrigger.LEAVE)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert result == FillResult(joined=0, created=0)
    return len(statements)


async def _unplaceable(session: AsyncSession, count: int) -> uuid.UUID:
    """Группа с `count` ждущими заявками, из которых ни одна ни с кем не совместима."""
    event = await make_event(session)
    for index in range(count):
        await _app(session, event.id, minutes=1000 * index)
    return event.id


@pytest.mark.parametrize("waiting", [1, 5, 40, 200])
async def test_queries_do_not_grow_with_waiting_applications(
    db_engine: AsyncEngine, db_session: AsyncSession, waiting: int
) -> None:
    event_id = await _unplaceable(db_session, waiting)

    queries = await count_statements(db_engine, db_session, event_id)

    # состояние группы читается один раз: свободные заявки и открытые трансферы
    assert queries <= 3


async def test_queries_with_open_transfer_do_not_grow_either(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    event_id = await _unplaceable(db_session, 200)
    first = await _app(db_session, event_id, minutes=40_000)
    second = await _app(db_session, event_id, minutes=40_002)
    assert await match_application(db_session, second) is not None
    assert first.status == ApplicationStatus.ASSIGNED

    queries = await count_statements(db_engine, db_session, event_id)

    assert queries <= 4
