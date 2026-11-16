import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
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
    Transfer,
    TransferStatus,
    Trip,
    User,
)
from app.observability.metrics import MatchTrigger
from app.schemas.application import ApplicationInput, MarksInput
from app.services.access import is_member
from app.services.audit import Actor, record
from app.services.jobs import enqueue_reconcile
from app.services.matching import (
    all_departed,
    fill_event,
    fits_current_transfer,
    leave_transfer,
    lock_event,
    mark_transfer_departed,
    match_application,
)

_FOREIGN_KEY_VIOLATION = "23503"

# Д7: ошибочное «Уехал» можно отменить в течение этого времени после отметки
UNDO_DEPARTURE_WINDOW = timedelta(minutes=30)

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
    leave_reason: AuditReason | None = None
    check_fit = False
    before: dict[str, object] = {}
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
        before = {name: getattr(application, name) for name in _TRACKED_FIELDS}
        application.trip_id = data.trip_id
        application.with_companion = data.with_companion
        application.baggage_count = data.baggage_count
        application.max_wait_minutes = data.max_wait_minutes
        if application.status == ApplicationStatus.ASSIGNED:
            # Д5: правка назначенной заявки; статус не трогаем, пока она не вышла из трансфера.
            # Совместимость проверяется после flush: FK гарантирует, что рейс существует
            check_fit = True
        elif data.seek_companions is True:
            application.status = ApplicationStatus.SEARCHING
        elif data.seek_companions is False:
            application.status = ApplicationStatus.SOLO
        elif application.status == ApplicationStatus.CANCELLED:
            application.status = ApplicationStatus.SEARCHING
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # рейс удалили между проверкой и записью
        if getattr(exc.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION:
            raise _trip_error() from exc
        raise
    if check_fit:
        if data.seek_companions is False:
            leave_reason = AuditReason.GO_SOLO
        elif not await fits_current_transfer(session, application):
            leave_reason = AuditReason.APPLICATION_CHANGED
    if leave_reason is not None:
        await leave_transfer(
            session,
            application,
            new_status=(
                ApplicationStatus.SOLO
                if leave_reason == AuditReason.GO_SOLO
                else ApplicationStatus.SEARCHING
            ),
            actor=_participant(user),
            reason=leave_reason,
        )
    changed_fields = [
        name for name in _TRACKED_FIELDS if before and getattr(application, name) != before[name]
    ]
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
    if leave_reason is not None:
        # освободившееся место и оставшихся пристраивает общий проход без самой заявки: она
        # не должна занять «чужое» место раньше ждущих (Д5); затем она ищет сама
        await fill_event(session, event.id, trigger=MatchTrigger.LEAVE, skip=application.id)
    elif check_fit and changed_fields:
        # заявка осталась в трансфере, а её поля изменились: освободившееся место (спутник,
        # багаж) занимает ждущая заявка; ручные трансферы `fill_event` не дополняет
        await fill_event(session, event.id, trigger=MatchTrigger.LEAVE)
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


async def mark_departed(
    session: AsyncSession,
    application: Application,
    *,
    actor: Actor,
    reason: AuditReason | None = None,
    close_transfer: bool = True,
) -> None:
    """Ставит заявке «Уехал» и пишет `marks_changed`; без commit. Заявка не должна быть уехавшей.

    Запоминает трансфер, из которого заявка уехала (Д7). Статус и состав трансфера не меняются.
    Если `close_transfer` и все участники трансфера уехали, он получает `departed`.
    """
    transfer_id = application.transfer_id
    application.departed_at = func.now()
    application.departed_from_transfer_id = transfer_id
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.MARKS_CHANGED,
        actor=actor,
        reason=reason,
        application=application,
        transfer_id=transfer_id,
        details={"at_meeting_point": application.at_meeting_point, "departed": True},
    )
    await session.flush()
    if close_transfer and transfer_id is not None and await all_departed(session, transfer_id):
        await mark_transfer_departed(session, application.event_id, transfer_id, actor)


async def _undo_departure(session: AsyncSession, application: Application, actor: Actor) -> None:
    """Отмена «Уехал» по Д7: назад в трансфер, если заявка в нём, иначе в поиск; без commit.

    Ветвится по тому, где заявка сейчас, а не по `departed_from_transfer_id`: у строк до миграции
    0007 он пуст, хотя заявка осталась в трансфере.
    """
    from_transfer_id = application.departed_from_transfer_id
    application.departed_at = None
    application.departed_from_transfer_id = None
    transfer_id = application.transfer_id
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.MARKS_CHANGED,
        actor=actor,
        reason=AuditReason.DEPARTURE_UNDONE,
        application=application,
        transfer_id=transfer_id,
        details={"at_meeting_point": application.at_meeting_point, "departed": False},
    )
    needs_match = False
    if transfer_id is not None:
        # заявка осталась в трансфере: он снова активен, если из-за неё считался уехавшим
        await session.execute(
            update(Transfer)
            .where(Transfer.id == transfer_id, Transfer.status == TransferStatus.DEPARTED)
            .values(status=TransferStatus.ACTIVE)
        )
        # пока заявка была «уехавшей», её рейс могли отменить или сдвинуть
        await enqueue_reconcile(session, event_id=application.event_id, trip_id=application.trip_id)
    elif from_transfer_id is not None:
        # трансфер распался, пока заявка ехала «сама»: снова в поиск
        _change_status(
            session,
            application,
            ApplicationStatus.SEARCHING,
            actor=actor,
            reason=AuditReason.DEPARTURE_UNDONE,
        )
        needs_match = True
    else:
        # уезжал не из трансфера: статус прежний, поиск продолжается, если он был
        needs_match = application.status == ApplicationStatus.SEARCHING
    await session.flush()
    if needs_match:
        await match_application(session, application, trigger=MatchTrigger.APPLICATION)


async def _within_undo_window(session: AsyncSession, application: Application) -> bool:
    """Отмена «Уехал» ещё возможна; окно считает БД: `departed_at` тоже поставлен её `now()`."""
    within = await session.scalar(
        select(Application.departed_at >= func.now() - UNDO_DEPARTURE_WINDOW).where(
            Application.id == application.id
        )
    )
    return bool(within)


async def set_my_marks(
    session: AsyncSession, event: Event, user: User, data: MarksInput
) -> Application:
    application = await _locked_application(session, event, user)
    if application.status == ApplicationStatus.CANCELLED:
        raise AppError(ErrorCode.APPLICATION_LOCKED, "Участие отменено", 409)
    if application.departed_at is not None:
        # повтор «Уехал» идемпотентен и момент отъезда не меняет; отмена — только в окне Д7
        if "at_meeting_point" in data.model_fields_set:
            raise _already_departed()
        if data.departed is True:
            return await _finish(session, event, user)
        if not await _within_undo_window(session, application):
            raise _already_departed()
        await _undo_departure(session, application, _participant(user))
        return await _finish(session, event, user)
    meeting_changed = (
        data.at_meeting_point is not None and data.at_meeting_point != application.at_meeting_point
    )
    if data.at_meeting_point is not None:
        application.at_meeting_point = data.at_meeting_point
    departing = bool(data.departed)
    if departing:
        await mark_departed(session, application, actor=_participant(user))
    elif meeting_changed:
        # отметка, не изменившая состояние, в историю не попадает
        record(
            session,
            event_id=event.id,
            action=AuditAction.MARKS_CHANGED,
            actor=_participant(user),
            application=application,
            details={"at_meeting_point": application.at_meeting_point, "departed": False},
        )
    return await _finish(session, event, user)
