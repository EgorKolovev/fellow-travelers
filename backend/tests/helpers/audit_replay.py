"""Восстановление составов трансферов по истории (docs/contracts/README.md, §11).

Правило: состав трансфера — `transfer_created.details.application_ids`, плюс `transfer_joined`,
минус `transfer_left`; у трансфера с `transfer_dissolved` состав пуст. Хелпер не знает про
устройство сценариев: он читает только записи `audit_log`, поэтому сверка с `applications`
ловит и забытую, и лишнюю запись.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Application, AuditAction, AuditLog


def replay_transfers(records: Iterable[AuditLog]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Составы всех трансферов из записей одной группы, от старых к новым (по `id`).

    Расформированные трансферы остаются в результате с пустым составом.
    """
    members: dict[uuid.UUID, set[uuid.UUID]] = {}
    for record in sorted(records, key=lambda row: row.id):
        transfer_id = record.transfer_id
        if transfer_id is None:
            continue
        match record.action:
            case AuditAction.TRANSFER_CREATED:
                assert transfer_id not in members, "трансфер создан дважды"
                members[transfer_id] = {uuid.UUID(raw) for raw in record.details["application_ids"]}
            case AuditAction.TRANSFER_JOINED:
                assert record.application_id is not None
                members[transfer_id].add(record.application_id)
            case AuditAction.TRANSFER_LEFT:
                assert record.application_id is not None
                members[transfer_id].discard(record.application_id)
            case AuditAction.TRANSFER_DISSOLVED:
                members[transfer_id] = set()
            case _:
                pass
    return members


async def current_compositions(
    session: AsyncSession, event_id: uuid.UUID
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Составы живых трансферов группы по `applications.transfer_id`."""
    rows = await session.execute(
        select(Application.transfer_id, Application.id).where(
            Application.event_id == event_id, Application.transfer_id.is_not(None)
        )
    )
    result: dict[uuid.UUID, set[uuid.UUID]] = {}
    for transfer_id, application_id in rows:
        assert transfer_id is not None
        result.setdefault(transfer_id, set()).add(application_id)
    return result


async def audit_rows(session: AsyncSession, event_id: uuid.UUID) -> list[AuditLog]:
    """Записи истории группы от старых к новым, свежие из БД."""
    stmt = (
        select(AuditLog)
        .where(AuditLog.event_id == event_id)
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.scalars(stmt)).all())


async def assert_history_matches_transfers(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Составы из истории совпадают с `applications.transfer_id`; распущенные трансферы пусты."""
    replayed = replay_transfers(await audit_rows(session, event_id))
    active = {transfer_id: ids for transfer_id, ids in replayed.items() if ids}
    assert active == await current_compositions(session, event_id), (
        "история не восстанавливает текущие составы трансферов"
    )
