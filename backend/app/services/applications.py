import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, ErrorCode
from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditReason,
    Event,
    Trip,
    User,
)
from app.observability.metrics import MatchTrigger
from app.schemas.application import ApplicationInput, MarksInput
from app.services.access import is_member
from app.services.audit import Actor, record
from app.services.matching import (
    all_departed,
    fill_event,
    leave_transfer,
    lock_event,
    mark_transfer_departed,
    match_application,
)

_FOREIGN_KEY_VIOLATION = "23503"

TRIP_NOT_FOUND = "Рейс не найден"

WITH_TRIP = (
    selectinload(Application.trip).selectinload(Trip.departure_location),
    selectinload(Application.trip).selectinload(Trip.arrival_location),
)

# поля заявки, чьи изменения при повторном PUT попадают в `application_updated.changed_fields`
_TRACKED_FIELDS = ("with_companion", "baggage_count", "max_wait_minutes", "trip_id", "status")


def _not_found() -> AppError:
    return AppError(ErrorCode.NOT_FOUND, "Заявка не найдена", 404)


def _already_departed() -> AppError:
    return AppError(ErrorCode.ALREADY_DEPARTED, "Вы уже уехали — изменить поездку нельзя", 409)


def _trip_error() -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR,
        "Проверьте введённые данные",
        422,
        {"fields": {"trip_id": TRIP_NOT_FOUND}},
    )


def _participant(user: User) -> Actor:
    return Actor(ActorKind.PARTICIPANT, user.id)


def _change_status(
    session: AsyncSession,
    application: Application,
    new_status: ApplicationStatus,
    *,
    actor: Actor,
    reason: AuditReason,
) -> None:
    """Меняет статус заявки вне трансфера и пишет `status_changed`; commit за сценарием."""
    old_status = application.status
    application.status = new_status
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.STATUS_CHANGED,
        actor=actor,
        reason=reason,
        application=application,
        details={"from_status": old_status.value, "to_status": new_status.value},
    )


async def _require_member(session: AsyncSession, event: Event, user: User) -> None:
    if not await is_member(session, event.id, user.id):
        raise AppError(
            ErrorCode.FORBIDDEN, "Чтобы подать заявку, вступите в группу по приглашению", 403
        )


async def find_application(
    session: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID
) -> Application | None:
    """Заявка пользователя в группе, свежая из БД, с рейсом и его локациями."""
    stmt = (
        select(Application)
        .where(Application.event_id == event_id, Application.user_id == user_id)
        .options(*WITH_TRIP)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_my_application(session: AsyncSession, event: Event, user: User) -> Application:
    application = await find_application(session, event.id, user.id)
    if application is None:
        raise _not_found()
    return application


async def _locked_application(session: AsyncSession, event: Event, user: User) -> Application:
    """Для действий над заявкой: участие, затем блокировка группы, затем свежая заявка."""
    await _require_member(session, event, user)
    await lock_event(session, event.id)
    return await get_my_application(session, event, user)


async def _finish(session: AsyncSession, event: Event, user: User) -> Application:
    """Коммит сценария и свежая заявка: ответ отражает состояние после подбора."""
    await session.commit()
    return await get_my_application(session, event, user)


async def _check_trip(session: AsyncSession, event: Event, trip_id: uuid.UUID) -> None:
    found = await session.scalar(
        select(Trip.id).where(Trip.id == trip_id, Trip.event_id == event.id)
    )
    if found is None:
        raise _trip_error()


async def upsert_my_application(
    session: AsyncSession, event: Event, user: User, data: ApplicationInput
) -> tuple[Application, bool]:
    """Создаёт или обновляет заявку и запускает подбор. Возвращает заявку и признак создания."""
    await _require_member(session, event, user)
    if user.phone is None:
        raise AppError(
            ErrorCode.PHONE_REQUIRED, "Чтобы отправить заявку, поделитесь телефоном в боте", 403
        )
    await _check_trip(session, event, data.trip_id)
    await lock_event(session, event.id)
    # перечитываем под блокировкой: параллельный первый PUT уже мог создать заявку
    application = await find_application(session, event.id, user.id)
    created = application is None
    changed_fields: list[str] = []
    if application is None:
        application = Application(
            event_id=event.id,
            user_id=user.id,
            trip_id=data.trip_id,
            with_companion=data.with_companion,
            baggage_count=data.baggage_count,
            max_wait_minutes=data.max_wait_minutes,
            status=(
                ApplicationStatus.SOLO
                if data.seek_companions is False
                else ApplicationStatus.SEARCHING
            ),
        )
        session.add(application)
    else:
        if application.departed_at is not None:
            raise _already_departed()
        if application.status == ApplicationStatus.ASSIGNED:
            raise AppError(
                ErrorCode.APPLICATION_LOCKED,
                "Попутчики уже назначены. Чтобы изменить заявку, сначала выйдите из трансфера",
                409,
            )
        before = {name: getattr(application, name) for name in _TRACKED_FIELDS}
        application.trip_id = data.trip_id
        application.with_companion = data.with_companion
        application.baggage_count = data.baggage_count
        application.max_wait_minutes = data.max_wait_minutes
        if data.seek_companions is True:
            application.status = ApplicationStatus.SEARCHING
        elif data.seek_companions is False:
            application.status = ApplicationStatus.SOLO
        elif application.status == ApplicationStatus.CANCELLED:
            application.status = ApplicationStatus.SEARCHING
        changed_fields = [
            name for name in _TRACKED_FIELDS if getattr(application, name) != before[name]
        ]
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # рейс удалили между проверкой и записью
        if getattr(exc.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION:
            raise _trip_error() from exc
        raise
    # статус в записи — до подбора: посадку в трансфер опишет transfer_created
    if created:
        record(
            session,
            event_id=event.id,
            action=AuditAction.APPLICATION_SUBMITTED,
            actor=_participant(user),
            application=application,
            details={"status": application.status.value},
        )
    elif changed_fields:
        # повторный PUT без изменений ничего не пишет
        record(
            session,
            event_id=event.id,
            action=AuditAction.APPLICATION_UPDATED,
            actor=_participant(user),
            application=application,
            details={"changed_fields": changed_fields, "status": application.status.value},
        )
    if application.status == ApplicationStatus.SEARCHING:
        await match_application(session, application, trigger=MatchTrigger.APPLICATION)
    return await _finish(session, event, user), created


async def go_solo(session: AsyncSession, event: Event, user: User) -> Application:
    application = await _locked_application(session, event, user)
    if application.departed_at is not None:
        raise _already_departed()
    if application.status == ApplicationStatus.ASSIGNED:
        await leave_transfer(
            session,
            application,
            new_status=ApplicationStatus.SOLO,
            actor=_participant(user),
            reason=AuditReason.GO_SOLO,
        )
        await fill_event(session, event.id, trigger=MatchTrigger.LEAVE)
    elif application.status != ApplicationStatus.SOLO:
        _change_status(
            session,
            application,
            ApplicationStatus.SOLO,
            actor=_participant(user),
            reason=AuditReason.GO_SOLO,
        )
    return await _finish(session, event, user)


async def resume_search(session: AsyncSession, event: Event, user: User) -> Application:
    application = await _locked_application(session, event, user)
    if application.departed_at is not None:
        raise _already_departed()
    if application.status in (ApplicationStatus.SOLO, ApplicationStatus.CANCELLED):
        _change_status(
            session,
            application,
            ApplicationStatus.SEARCHING,
            actor=_participant(user),
            reason=AuditReason.RESUME_SEARCH,
        )
        await session.flush()
        await match_application(session, application, trigger=MatchTrigger.APPLICATION)
    return await _finish(session, event, user)


async def cancel_my_application(session: AsyncSession, event: Event, user: User) -> Application:
    application = await _locked_application(session, event, user)
    if application.departed_at is not None:
        raise _already_departed()
    application.at_meeting_point = False
    if application.status == ApplicationStatus.ASSIGNED:
        await leave_transfer(
            session,
            application,
            new_status=ApplicationStatus.CANCELLED,
            actor=_participant(user),
            reason=AuditReason.CANCEL,
        )
        await fill_event(session, event.id, trigger=MatchTrigger.LEAVE)
    elif application.status != ApplicationStatus.CANCELLED:
        _change_status(
            session,
            application,
            ApplicationStatus.CANCELLED,
            actor=_participant(user),
            reason=AuditReason.CANCEL,
        )
    return await _finish(session, event, user)


async def set_my_marks(
    session: AsyncSession, event: Event, user: User, data: MarksInput
) -> Application:
    application = await _locked_application(session, event, user)
    if application.status == ApplicationStatus.CANCELLED:
        raise AppError(ErrorCode.APPLICATION_LOCKED, "Участие отменено", 409)
    if application.departed_at is not None:
        # повтор «Уехал» идемпотентен, любое изменение — нет; момент отъезда не меняется
        if data.departed is True and "at_meeting_point" not in data.model_fields_set:
            return await _finish(session, event, user)
        raise _already_departed()
    meeting_changed = (
        data.at_meeting_point is not None and data.at_meeting_point != application.at_meeting_point
    )
    if data.at_meeting_point is not None:
        application.at_meeting_point = data.at_meeting_point
    departing = bool(data.departed)
    if departing:
        application.departed_at = func.now()
    if meeting_changed or departing:
        # отметка, не изменившая состояние, в историю не попадает
        record(
            session,
            event_id=event.id,
            action=AuditAction.MARKS_CHANGED,
            actor=_participant(user),
            application=application,
            details={"at_meeting_point": application.at_meeting_point, "departed": departing},
        )
    if departing:
        await session.flush()
        transfer_id = application.transfer_id
        if transfer_id is not None and await all_departed(session, transfer_id):
            await mark_transfer_departed(session, event.id, transfer_id, _participant(user))
    return await _finish(session, event, user)
