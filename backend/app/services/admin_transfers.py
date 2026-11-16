import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.matching import fits_capacity, fits_waits
from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditReason,
    Event,
    Transfer,
    TransferOrigin,
    TransferStatus,
    Trip,
    TripStatus,
    User,
)
from app.observability.metrics import MatchTrigger
from app.services.audit import Actor, record
from app.services.matching import _candidate, fill_event, leave_transfer, lock_event
from app.services.transfers import load_transfer

_FIELD = "application_ids"


def _invalid(message: str) -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR, "Проверьте введённые данные", 422, {"fields": {_FIELD: message}}
    )


def _already_departed() -> AppError:
    return AppError(
        ErrorCode.ALREADY_DEPARTED, "У одной из заявок стоит «Уехал» — состав изменить нельзя", 409
    )


async def _load_applications(
    session: AsyncSession, event_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Application, Trip]]:
    """Заявки группы вместе с рейсами, свежие из БД (под блокировкой группы)."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Application, Trip)
            .join(Trip, Trip.id == Application.trip_id)
            .where(Application.event_id == event_id, Application.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    return {application.id: (application, trip) for application, trip in rows}


def _check_list(ids: Sequence[uuid.UUID], *, min_size: int) -> None:
    if len(ids) < min_size or len(ids) == 1:
        raise _invalid("Нужно выбрать минимум две заявки")
    if len(set(ids)) != len(ids):
        raise _invalid("Заявки в списке не должны повторяться")


def _check_applications(
    ids: Sequence[uuid.UUID], found: dict[uuid.UUID, tuple[Application, Trip]]
) -> None:
    """422: заявка не из этой группы или в статусе, который в трансфер не ставят."""
    for application_id in ids:
        if application_id not in found:
            raise _invalid("Заявка не найдена в этой группе")
    for application_id in ids:
        status = found[application_id][0].status
        if status in (ApplicationStatus.SOLO, ApplicationStatus.CANCELLED):
            raise _invalid("В трансфер нельзя поставить заявку «Еду сам» или отменённую")


def _check_composition(members: Sequence[tuple[Application, Trip]]) -> None:
    """409: вместимость по всем заявкам состава (включая уехавших), затем аэропорт и ожидание.

    Аэропорт, ожидание и отменённый рейс проверяются только у тех, кто ещё не уехал.
    """
    if not members:
        return
    waiting = [(a, t) for a, t in members if a.departed_at is None]
    if not fits_capacity([_candidate(a, t) for a, t in members]):
        raise AppError(
            ErrorCode.CAPACITY_EXCEEDED,
            "В машину не помещаются все пассажиры и багаж: не больше 3 человек и 2 мест багажа",
            409,
        )
    if any(trip.status == TripStatus.CANCELLED for _, trip in waiting):
        raise AppError(
            ErrorCode.INCOMPATIBLE_TRANSFER,
            "Рейс одной из заявок отменён: такую заявку нельзя поставить в трансфер",
            409,
        )
    if not fits_waits([_candidate(a, t) for a, t in waiting]):
        raise AppError(
            ErrorCode.INCOMPATIBLE_TRANSFER,
            "Заявки не подходят друг другу: разные аэропорты или время ожидания не сходится",
            409,
        )


def _admin(user: User) -> Actor:
    return Actor(ActorKind.ADMIN, user.id)


async def _leave_old_transfer(
    session: AsyncSession, application: Application, actor: Actor
) -> None:
    """Заявка выходит из прежнего трансфера в поиск; без трансфера ничего не делает."""
    if application.transfer_id is not None:
        await leave_transfer(
            session,
            application,
            new_status=ApplicationStatus.SEARCHING,
            actor=actor,
            reason=AuditReason.ADMIN_EDIT,
        )


async def create_transfer(
    session: AsyncSession, event: Event, user: User, application_ids: Sequence[uuid.UUID]
) -> Transfer:
    """Ручной трансфер из заявок (CAP-06). Все проверки до первого изменения; commit в конце."""
    ids = list(application_ids)
    _check_list(ids, min_size=2)
    await lock_event(session, event.id)
    found = await _load_applications(session, event.id, ids)
    _check_applications(ids, found)
    if any(found[i][0].departed_at is not None for i in ids):
        raise _already_departed()
    _check_composition([found[i] for i in ids])

    actor = _admin(user)
    for application_id in ids:
        await _leave_old_transfer(session, found[application_id][0], actor)
    transfer = Transfer(event_id=event.id, origin=TransferOrigin.ADMIN)
    session.add(transfer)
    await session.flush()
    for application_id in ids:
        # status и transfer_id меняются вместе: CHECK assigned_has_transfer цел
        found[application_id][0].status = ApplicationStatus.ASSIGNED
        found[application_id][0].transfer_id = transfer.id
    await session.flush()
    record(
        session,
        event_id=event.id,
        action=AuditAction.TRANSFER_CREATED,
        actor=actor,
        reason=AuditReason.ADMIN_EDIT,
        transfer_id=transfer.id,
        details={"application_ids": [str(i) for i in ids]},
    )
    transfer_id = transfer.id
    await fill_event(session, event.id, trigger=MatchTrigger.LEAVE)
    await session.commit()
    created = await load_transfer(session, transfer_id)
    assert created is not None
    return created


async def set_transfer_members(
    session: AsyncSession,
    event: Event,
    user: User,
    transfer_id: uuid.UUID,
    application_ids: Sequence[uuid.UUID],
) -> Transfer:
    """Задаёт состав трансфера целиком (CAP-11); пустой список расформировывает его.

    Все проверки до первого изменения; commit в конце. Уехавшие заявки, уже стоящие в этом
    трансфере, можно оставить в списке, но нельзя убрать; места и багаж считаются и по ним, а
    аэропорт и ожидание — только по ждущим (как в `reconcile_event`).
    """
    ids = list(application_ids)
    _check_list(ids, min_size=0)
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
    found = await _load_applications(session, event.id, ids)
    _check_applications(ids, found)
    if transfer.status == TransferStatus.DEPARTED:
        raise _already_departed()
    current = list(
        (
            await session.scalars(
                select(Application)
                .where(Application.transfer_id == transfer.id)
                .order_by(Application.created_at, Application.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    current_ids = {a.id for a in current}
    wanted = set(ids)
    if any(a.departed_at is not None and a.id not in wanted for a in current):
        raise _already_departed()
    if any(found[i][0].departed_at is not None and i not in current_ids for i in ids):
        raise _already_departed()
    _check_composition([found[i] for i in ids])

    actor = _admin(user)
    transfer.origin = TransferOrigin.ADMIN
    snapshot = Transfer(
        id=transfer.id,
        event_id=transfer.event_id,
        status=transfer.status,
        created_at=transfer.created_at,
    )
    # сначала входят новые, потом уходят лишние: пока в составе остаётся минимум двое
    # (`len(ids) >= 2`), `leave_transfer` трансфер не расформирует
    for application_id in ids:
        if application_id in current_ids:
            continue
        application = found[application_id][0]
        await _leave_old_transfer(session, application, actor)
        application.status = ApplicationStatus.ASSIGNED
        application.transfer_id = transfer.id
        await session.flush()
        record(
            session,
            event_id=event.id,
            action=AuditAction.TRANSFER_JOINED,
            actor=actor,
            reason=AuditReason.ADMIN_EDIT,
            application=application,
            transfer_id=transfer.id,
        )
    for application in current:
        if application.id not in wanted:
            # при пустом списке последний `leave_transfer` расформирует трансфер и отпустит остаток
            await _leave_old_transfer(session, application, actor)
    await fill_event(session, event.id, trigger=MatchTrigger.LEAVE)
    await session.commit()
    if not ids:
        # трансфера в БД уже нет; ответ — тот же id с пустым составом
        snapshot.applications = []
        return snapshot
    updated = await load_transfer(session, transfer_id)
    assert updated is not None
    return updated
