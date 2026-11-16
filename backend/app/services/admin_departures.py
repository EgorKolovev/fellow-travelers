import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditReason,
    Event,
    Transfer,
    TransferStatus,
    User,
)
from app.services.applications import WITH_TRIP, mark_departed
from app.services.audit import Actor
from app.services.matching import lock_event, mark_transfer_departed
from app.services.transfers import load_transfer


def _admin(user: User) -> Actor:
    return Actor(ActorKind.ADMIN, user.id)


def _locked() -> AppError:
    return AppError(ErrorCode.APPLICATION_LOCKED, "Участие отменено", 409)


async def mark_application_departed(
    session: AsyncSession, event: Event, user: User, application_id: uuid.UUID
) -> Application:
    """Админ отмечает «Уехал» заявке (CAP-08); повтор идемпотентен. Commit в конце."""
    await lock_event(session, event.id)
    application = (
        await session.execute(
            select(Application)
            .where(Application.id == application_id, Application.event_id == event.id)
            .options(*WITH_TRIP)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if application is None:
        raise AppError(ErrorCode.NOT_FOUND, "Заявка не найдена", 404)
    if application.status == ApplicationStatus.CANCELLED:
        raise _locked()
    if application.departed_at is None:
        await mark_departed(session, application, actor=_admin(user), reason=AuditReason.ADMIN_EDIT)
    await session.commit()
    refreshed = (
        await session.execute(
            select(Application)
            .where(Application.id == application_id)
            .options(*WITH_TRIP)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return refreshed


async def mark_transfer_members_departed(
    session: AsyncSession, event: Event, user: User, transfer_id: uuid.UUID
) -> Transfer:
    """Админ отмечает «Уехал» всему трансферу; повтор идемпотентен. Commit в конце."""
    await lock_event(session, event.id)
    transfer = (
        await session.execute(
            select(Transfer)
            .where(Transfer.id == transfer_id, Transfer.event_id == event.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise AppError(ErrorCode.NOT_FOUND, "Трансфер не найден", 404)
    actor = _admin(user)
    members = (
        await session.scalars(
            select(Application)
            .where(Application.transfer_id == transfer_id)
            .order_by(Application.created_at, Application.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    for application in members:
        if application.departed_at is None:
            # трансфер закрывается один раз, после всех отметок
            await mark_departed(
                session,
                application,
                actor=actor,
                reason=AuditReason.ADMIN_EDIT,
                close_transfer=False,
            )
    if transfer.status != TransferStatus.DEPARTED:
        await mark_transfer_departed(session, event.id, transfer_id, actor)
    await session.commit()
    result = await load_transfer(session, transfer_id)
    assert result is not None
    return result
