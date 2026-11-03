"""Помощники для тестов фоновых заданий."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BackgroundJob


def shared_session_factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий на том же соединении и во внешней транзакции, что и `session`.

    `process_next` открывает свою сессию и коммитит её: на общем соединении коммит освобождает
    только savepoint, поэтому тест видит данные `db_session` и откатывает всё в конце. Для гонок
    между соединениями такая фабрика не годится: транзакция у обеих сессий одна.
    """
    bind = session.bind
    assert bind is not None
    return async_sessionmaker(
        bind=bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


async def jobs_of(session: AsyncSession, event_id: uuid.UUID) -> list[BackgroundJob]:
    """Задания группы, свежие из БД, от старых к новым."""
    stmt = (
        select(BackgroundJob)
        .where(BackgroundJob.event_id == event_id)
        .order_by(BackgroundJob.created_at, BackgroundJob.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.scalars(stmt)).all())


async def job_count(session: AsyncSession) -> int:
    result = await session.scalar(select(func.count()).select_from(BackgroundJob))
    assert result is not None
    return result
