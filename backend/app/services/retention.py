"""Удаление данных группы через 7 суток после её окончания (Д11, CAP-02).

Срок: `now >= max(конец дня ends_on, последняя отметка «Уехал», created_at и updated_at группы)
+ 7 суток`. Создание и любая правка группы дают свои 7 суток, поэтому `ends_on`, поставленный
задним числом (опечатка или решение админа), не удаляет группу в ближайший час. У группы нет своего
часового пояса, поэтому «конец дня ends_on» берётся по самому позднему поясу на Земле (UTC−12):
`(ends_on + 1 день) 00:00 UTC + 12 ч`. Так группа не удаляется раньше срока ни в одном поясе.
Нет отметок «Уехал» — срок считается от `ends_on`, `created_at` и `updated_at`.

Удаляется строка `events`; участие, заявки, трансферы, рейсы, история, задания, уведомления и
приглашение уходят каскадом БД. Триггер `audit_log` пропускает каскад (`pg_trigger_depth() >= 2`).
`applications.trip_id` и `transfer_id` без `ondelete` (NO ACTION) каскаду не мешают: проверка
идёт в конце оператора, когда заявки уже удалены.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

RETENTION_PERIOD = timedelta(days=7)

# Момент, когда группу пора удалять; `e` — строка events. GREATEST пропускает NULL, поэтому
# без отметок «Уехал» остаются конец дня ends_on по UTC−12 и created_at/updated_at группы
# (`events.updated_at` обновляется при любой правке строки: `onupdate=now()`).
_DEADLINE = """
    GREATEST(
        ((e.ends_on + 1)::timestamp AT TIME ZONE 'UTC') + interval '12 hours',
        (SELECT max(a.departed_at) FROM applications a WHERE a.event_id = e.id),
        e.created_at,
        e.updated_at
    ) + CAST(:period AS interval)
"""

_EXPIRED = text(
    f"""
    SELECT e.id FROM events e
    WHERE {_DEADLINE} <= :now
    ORDER BY {_DEADLINE}, e.id
    LIMIT :limit
    """
)

# условие срока повторяется в DELETE: группе, которой после выборки продлили ends_on или
# отметили «Уехал», срок пересчитывается и она остаётся
_DELETE = text(
    f"""
    DELETE FROM events e
    WHERE e.id = :id AND {_DEADLINE} <= :now
    RETURNING e.id
    """
)


async def expired_event_ids(
    session: AsyncSession, now: datetime, *, limit: int = 50
) -> list[uuid.UUID]:
    """Группы с истёкшим сроком, самые старые первыми. Только чтение."""
    result = await session.execute(
        _EXPIRED, {"now": now, "period": RETENTION_PERIOD, "limit": limit}
    )
    return [row[0] for row in result]


async def purge_event(
    session: AsyncSession, event_id: uuid.UUID, *, now: datetime | None = None
) -> bool:
    """Удаляет группу, если срок всё ещё истёк; сама делает commit. True — группа удалена."""
    moment = now if now is not None else datetime.now(UTC)
    result = await session.execute(
        _DELETE, {"id": event_id, "now": moment, "period": RETENTION_PERIOD}
    )
    deleted = result.first() is not None
    await session.commit()
    return deleted


async def purge_expired(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    """Удаляет до `limit` просроченных групп, каждую в своей транзакции. Возвращает их число."""
    moment = now if now is not None else datetime.now(UTC)
    async with session_factory() as session:
        event_ids = await expired_event_ids(session, moment, limit=limit)
        await session.rollback()  # закрыть транзакцию чтения
    purged = 0
    for event_id in event_ids:
        try:
            async with session_factory() as session:
                try:
                    if await purge_event(session, event_id, now=moment):
                        purged += 1
                except BaseException:
                    await session.rollback()
                    raise
        except Exception as exc:
            logger.error("Не удалось удалить группу %s: %s", event_id, type(exc).__name__)
    logger.info("Удалено групп: %d", purged)
    return purged
