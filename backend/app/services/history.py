import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import AuditLog, User
from app.schemas.history import HistoryEntry, HistoryPage

# audit_log.id — bigint; число больше БД отвергнет, хотя записей старше такого seq всё равно нет
BIGINT_MAX = 2**63 - 1


async def list_history(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    limit: int,
    before_seq: int | None,
) -> HistoryPage:
    """Страница истории группы от новых записей к старым. Только чтение, один запрос.

    Автор и субъект подтягиваются LEFT JOIN-ами: их может не быть (системная запись, запись о
    рейсе целиком, удалённый профиль). Берём `limit + 1` записей: лишняя говорит, что есть
    записи старше; `next_before_seq` — seq последней записи страницы.
    """
    actor = aliased(User)
    subject = aliased(User)
    stmt = (
        select(AuditLog, actor, subject)
        .outerjoin(actor, actor.id == AuditLog.actor_user_id)
        .outerjoin(subject, subject.id == AuditLog.subject_user_id)
        .where(AuditLog.event_id == event_id)
        .order_by(AuditLog.id.desc())
        .limit(limit + 1)
    )
    if before_seq is not None:
        stmt = stmt.where(AuditLog.id < min(before_seq, BIGINT_MAX))
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    return HistoryPage(
        items=[HistoryEntry.build(record, who, whom) for record, who, whom in page],
        next_before_seq=page[-1][0].id if len(rows) > limit else None,
    )
