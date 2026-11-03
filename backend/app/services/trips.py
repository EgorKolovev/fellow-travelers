import uuid
from datetime import datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, ErrorCode
from app.models import (
    Application,
    AuditAction,
    AuditReason,
    Event,
    Location,
    LocationKind,
    TransportMode,
    Trip,
    TripSource,
    User,
)
from app.schemas.trip import TripInput, TripUpdate, normalize_number
from app.services.access import get_event_for_member
from app.services.audit import Actor, iso, record
from app.services.jobs import enqueue_reconcile
from app.services.matching import lock_event
from app.timezones import from_local_or_aware

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"
_UNIQUE_TRIP = "uq_trips_event_mode_number_departure"

LOCATION_NOT_FOUND = "Аэропорт не найден"
SAME_AIRPORTS = "Аэропорты вылета и прилёта должны различаться"
ARRIVAL_ORDER = "Прилёт должен быть позже вылета"
ESTIMATED_ORDER = "Расчётное прибытие должно быть позже вылета"
DUPLICATE_TRIP = "Такой рейс уже добавлен"

_WITH_LOCATIONS = (selectinload(Trip.departure_location), selectinload(Trip.arrival_location))

# поля рейса, чьи изменения попадают в `trip_updated.changed_fields`, в порядке модели
_AUDITED_FIELDS = (
    "number",
    "departure_location_id",
    "arrival_location_id",
    "scheduled_departure",
    "scheduled_arrival",
    "estimated_arrival",
    "source",
)


def _fields_error(fields: dict[str, str]) -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR, "Проверьте введённые данные", 422, {"fields": fields}
    )


def _trip_not_found() -> AppError:
    return AppError(ErrorCode.NOT_FOUND, "Рейс не найден", 404)


def _in_use() -> AppError:
    return AppError(ErrorCode.TRIP_IN_USE, "На рейс уже есть заявки — его нельзя удалить", 409)


def _violation(exc: IntegrityError, sqlstate: str, constraint: str | None = None) -> bool:
    if getattr(exc.orig, "sqlstate", None) != sqlstate:
        return False
    if constraint is None:
        return True
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(exc.orig, "constraint_name", None) or getattr(cause, "constraint_name", None)
    return bool(name == constraint)


async def _load_trip(session: AsyncSession, trip_id: uuid.UUID) -> Trip:
    """Рейс с локациями, свежий из БД (после коммита серверные поля устарели).

    Рейс, удалённый параллельно, — 404: админ мог удалить его, пока правка ждала блокировку.
    """
    stmt = (
        select(Trip)
        .where(Trip.id == trip_id)
        .options(*_WITH_LOCATIONS)
        .execution_options(populate_existing=True)
    )
    trip = (await session.execute(stmt)).scalar_one_or_none()
    if trip is None:
        raise _trip_not_found()
    return trip


async def list_event_trips(session: AsyncSession, event_id: uuid.UUID) -> list[Trip]:
    stmt = (
        select(Trip)
        .where(Trip.event_id == event_id)
        .options(*_WITH_LOCATIONS)
        .order_by(Trip.scheduled_arrival, Trip.number, Trip.id)
    )
    return list((await session.scalars(stmt)).all())


async def get_trip_for_admin(session: AsyncSession, trip_id: uuid.UUID, user: User) -> Trip:
    """Рейс группы, где пользователь админ. Нет рейса или группы — 404, участник — 403."""
    trip = await session.get(Trip, trip_id)
    if trip is None:
        raise _trip_not_found()
    try:
        event = await get_event_for_member(session, trip.event_id, user)
    except AppError as exc:
        if exc.status_code == 404:
            raise _trip_not_found() from exc
        raise
    if event.admin_id != user.id:
        raise AppError(ErrorCode.FORBIDDEN, "Управлять группой может только её создатель", 403)
    return trip


async def _find_airport(session: AsyncSession, location_id: uuid.UUID) -> Location | None:
    location = await session.get(Location, location_id)
    if location is None or location.kind != LocationKind.AIRPORT:
        return None
    return location


async def _resolve_airports(
    session: AsyncSession, departure_id: uuid.UUID, arrival_id: uuid.UUID
) -> tuple[Location, Location]:
    departure = await _find_airport(session, departure_id)
    arrival = await _find_airport(session, arrival_id)
    errors: dict[str, str] = {}
    if departure is None:
        errors["departure_location_id"] = LOCATION_NOT_FOUND
    if arrival is None:
        errors["arrival_location_id"] = LOCATION_NOT_FOUND
    if departure is None or arrival is None:
        raise _fields_error(errors)
    if departure.id == arrival.id:
        raise _fields_error({"arrival_location_id": SAME_AIRPORTS})
    return departure, arrival


def _check_times(scheduled_departure: datetime, scheduled_arrival: datetime) -> None:
    if scheduled_arrival <= scheduled_departure:
        raise _fields_error({"scheduled_arrival": ARRIVAL_ORDER})


async def create_trip(session: AsyncSession, event: Event, data: TripInput) -> Trip:
    departure, arrival = await _resolve_airports(
        session, data.departure_location_id, data.arrival_location_id
    )
    scheduled_departure = from_local_or_aware(data.scheduled_departure, departure.timezone)
    scheduled_arrival = from_local_or_aware(data.scheduled_arrival, arrival.timezone)
    _check_times(scheduled_departure, scheduled_arrival)
    trip = Trip(
        event_id=event.id,
        mode=TransportMode.FLIGHT,
        number=normalize_number(data.number),
        departure_location_id=departure.id,
        arrival_location_id=arrival.id,
        scheduled_departure=scheduled_departure,
        scheduled_arrival=scheduled_arrival,
        source=TripSource.MANUAL,
    )
    try:
        async with session.begin_nested():
            session.add(trip)
            await session.flush()
    except IntegrityError as exc:
        if _violation(exc, _UNIQUE_VIOLATION, _UNIQUE_TRIP):
            raise _fields_error({"number": DUPLICATE_TRIP}) from exc
        raise
    await session.commit()
    return await _load_trip(session, trip.id)


async def update_trip(session: AsyncSession, trip: Trip, data: TripUpdate, actor: Actor) -> Trip:
    """Исправление рейса админом; в историю пишется, только если рейс действительно изменился.

    Первым берётся `lock_event` группы, затем рейс перечитывается: роутер читал его до блокировки,
    и параллельная правка могла успеть раньше. Снимок «до» для истории берётся после перечитывания.
    Если изменилось расчётное прибытие или аэропорт прилёта, в той же транзакции ставится задание
    пересчёта трансферов: воркер исключит заявки, которым новое время не подходит. Ошибка
    валидации оставляет БД как была: ни правки, ни записи истории, ни задания.
    """
    await lock_event(session, trip.event_id)
    trip = await _load_trip(session, trip.id)
    changes = {name: getattr(data, name) for name in data.model_fields_set}
    before = {name: getattr(trip, name) for name in _AUDITED_FIELDS}
    arrival_before = trip.effective_arrival
    departure, arrival = await _resolve_airports(
        session,
        changes.get("departure_location_id", trip.departure_location_id),
        changes.get("arrival_location_id", trip.arrival_location_id),
    )
    # переданное время без смещения — в таймзоне итогового аэропорта; не переданное не трогаем
    scheduled_departure = trip.scheduled_departure
    scheduled_arrival = trip.scheduled_arrival
    estimated_arrival = trip.estimated_arrival
    if "scheduled_departure" in changes:
        scheduled_departure = from_local_or_aware(
            changes["scheduled_departure"], departure.timezone
        )
    if "scheduled_arrival" in changes:
        scheduled_arrival = from_local_or_aware(changes["scheduled_arrival"], arrival.timezone)
    if "estimated_arrival" in changes:
        value = changes["estimated_arrival"]
        estimated_arrival = None if value is None else from_local_or_aware(value, arrival.timezone)
    _check_times(scheduled_departure, scheduled_arrival)
    if estimated_arrival is not None and estimated_arrival <= scheduled_departure:
        raise _fields_error({"estimated_arrival": ESTIMATED_ORDER})

    try:
        # правка внутри savepoint: begin_nested() сначала сбрасывает накопленное, и отказ БД
        # при записи (дубль номера) откатил бы всю транзакцию, а не только эту правку
        async with session.begin_nested():
            trip.departure_location_id = departure.id
            trip.arrival_location_id = arrival.id
            trip.scheduled_departure = scheduled_departure
            trip.scheduled_arrival = scheduled_arrival
            trip.estimated_arrival = estimated_arrival
            if "number" in changes:
                trip.number = normalize_number(changes["number"])
            trip.source = TripSource.MANUAL
            await session.flush()
    except IntegrityError as exc:
        if _violation(exc, _UNIQUE_VIOLATION, _UNIQUE_TRIP):
            raise _fields_error({"number": DUPLICATE_TRIP}) from exc
        raise
    changed_fields = [name for name in _AUDITED_FIELDS if getattr(trip, name) != before[name]]
    if trip.effective_arrival != arrival_before or "arrival_location_id" in changed_fields:
        await enqueue_reconcile(session, event_id=trip.event_id, trip_id=trip.id)
    if changed_fields:
        record(
            session,
            event_id=trip.event_id,
            action=AuditAction.TRIP_UPDATED,
            actor=actor,
            reason=AuditReason.ADMIN_EDIT,
            trip_id=trip.id,
            details={
                "changed_fields": changed_fields,
                "effective_arrival_before": iso(arrival_before),
                "effective_arrival_after": iso(trip.effective_arrival),
            },
        )
    await session.commit()
    return await _load_trip(session, trip.id)


async def delete_trip(session: AsyncSession, trip: Trip) -> None:
    if await session.scalar(select(exists().where(Application.trip_id == trip.id))):
        raise _in_use()
    try:
        async with session.begin_nested():
            await session.execute(delete(Trip).where(Trip.id == trip.id))
    except IntegrityError as exc:
        # заявка появилась между проверкой и удалением
        if _violation(exc, _FOREIGN_KEY_VIOLATION):
            raise _in_use() from exc
        raise
    await session.commit()
