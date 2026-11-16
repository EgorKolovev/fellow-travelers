import asyncio
import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, ErrorCode
from app.flights.provider import FlightProvider, ProviderFlight, ProviderUnavailableError
from app.models import (
    AuditAction,
    AuditReason,
    Location,
    LocationKind,
    TransportMode,
    Trip,
    TripSource,
    TripStatus,
    User,
)
from app.schemas.trip import normalize_number
from app.services.access import get_event_for_member
from app.services.audit import SYSTEM, iso, record
from app.services.jobs import enqueue_reconcile
from app.services.matching import lock_event

logger = logging.getLogger(__name__)

# сколько ждём ответ поставщика; константа модуля, чтобы тест мог её уменьшить
PROVIDER_TIMEOUT_SECONDS = 10.0

PROVIDER_UNAVAILABLE = (
    "Поиск рейсов временно недоступен. Добавьте рейс вручную или попробуйте позже"
)


def _unavailable() -> AppError:
    return AppError(ErrorCode.FLIGHT_PROVIDER_UNAVAILABLE, PROVIDER_UNAVAILABLE, 503)


async def _query_provider(
    provider: FlightProvider | None, number: str, departure_date: date
) -> list[ProviderFlight]:
    if provider is None:
        raise _unavailable()
    try:
        async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
            return await provider.search(number, departure_date)
    except (ProviderUnavailableError, TimeoutError) as exc:
        raise _unavailable() from exc


async def _airports(session: AsyncSession, flights: list[ProviderFlight]) -> dict[str, Location]:
    codes = {f.departure_iata for f in flights} | {f.arrival_iata for f in flights}
    stmt = select(Location).where(Location.kind == LocationKind.AIRPORT, Location.code.in_(codes))
    return {location.code: location for location in await session.scalars(stmt)}


# поля рейса, чьи изменения попадают в `trip_updated.changed_fields`, в порядке модели
_UPDATE_FIELDS = (
    "number",
    "departure_location_id",
    "arrival_location_id",
    "scheduled_departure",
    "scheduled_arrival",
    "estimated_arrival",
    "status",
)


async def apply_update(session: AsyncSession, trip: Trip, flight: ProviderFlight) -> bool:
    """Применяет данные поставщика к рейсу; True, если рейс изменился. Только flush.

    Вызывать под `lock_event` группы, с рейсом, свежим из БД. Сравнение идёт с текущими
    значениями в БД, поэтому ответ поставщика перезаписывает и ручную правку админа (CAP-05).
    Аэропорт, которого нет в справочнике, не применяется (WARNING с кодами, без номера рейса),
    остальные поля применяются. Без изменений ничего не пишется: ни истории, ни задания.
    При изменениях `source=provider`, пишется `trip_updated` от системы с причиной
    `provider_update`; если изменились расчётное прибытие, аэропорт прилёта или рейс стал
    отменённым либо перестал быть таковым, ставится пересчёт трансферов группы.
    """
    airports = await _airports(session, [flight])
    departure_id = trip.departure_location_id
    arrival_id = trip.arrival_location_id
    departure = airports.get(flight.departure_iata)
    arrival = airports.get(flight.arrival_iata)
    if departure is None:
        logger.warning(
            "Обновление рейса: аэропорта вылета нет в справочнике (%s)", flight.departure_iata
        )
    else:
        departure_id = departure.id
    if arrival is None:
        logger.warning(
            "Обновление рейса: аэропорта прилёта нет в справочнике (%s)", flight.arrival_iata
        )
    else:
        arrival_id = arrival.id
    if flight.scheduled_arrival <= flight.scheduled_departure:
        logger.warning(
            "Обновление рейса пропущено: прилёт не позже вылета (%s -> %s)",
            flight.departure_iata,
            flight.arrival_iata,
        )
        return False

    new_values = {
        "number": flight.number,
        "departure_location_id": departure_id,
        "arrival_location_id": arrival_id,
        "scheduled_departure": flight.scheduled_departure,
        "scheduled_arrival": flight.scheduled_arrival,
        "estimated_arrival": flight.estimated_arrival,
        "status": flight.status,
    }
    before = {name: getattr(trip, name) for name in _UPDATE_FIELDS}
    changed_fields = [name for name in _UPDATE_FIELDS if new_values[name] != before[name]]
    if not changed_fields:
        return False
    arrival_before = trip.effective_arrival
    for name in changed_fields:
        setattr(trip, name, new_values[name])
    trip.source = TripSource.PROVIDER
    await session.flush()
    was_cancelled = before["status"] == TripStatus.CANCELLED
    is_cancelled = trip.status == TripStatus.CANCELLED
    if (
        trip.effective_arrival != arrival_before
        or "arrival_location_id" in changed_fields
        or was_cancelled != is_cancelled
    ):
        await enqueue_reconcile(session, event_id=trip.event_id, trip_id=trip.id)
    record(
        session,
        event_id=trip.event_id,
        action=AuditAction.TRIP_UPDATED,
        actor=SYSTEM,
        reason=AuditReason.PROVIDER_UPDATE,
        trip_id=trip.id,
        details={
            "changed_fields": changed_fields,
            "effective_arrival_before": iso(arrival_before),
            "effective_arrival_after": iso(trip.effective_arrival),
        },
    )
    return True


async def _save(
    session: AsyncSession,
    event_id: uuid.UUID,
    flight: ProviderFlight,
    departure: Location,
    arrival: Location,
) -> Trip:
    """Рейс поставщика в группе: сохранённый обновляем, ручной усыновляем, иначе создаём."""
    existing = await session.scalar(
        select(Trip)
        .where(Trip.event_id == event_id, Trip.provider_ref == flight.provider_ref)
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        await apply_update(session, existing, flight)
        return existing
    manual = await session.scalar(
        select(Trip)
        .where(
            Trip.event_id == event_id,
            Trip.mode == TransportMode.FLIGHT,
            Trip.number == flight.number,
            Trip.scheduled_departure == flight.scheduled_departure,
        )
        .execution_options(populate_existing=True)
    )
    if manual is not None:
        # данные пишет apply_update, чтобы в истории осталась запись о том, что поменялось
        manual.provider_ref = flight.provider_ref
        manual.source = TripSource.PROVIDER
        manual.next_poll_at = datetime.now(UTC)  # опрос в воркере подхватит рейс сразу
        await session.flush()
        await apply_update(session, manual, flight)
        return manual
    trip = Trip(
        event_id=event_id,
        mode=TransportMode.FLIGHT,
        number=flight.number,
        departure_location_id=departure.id,
        arrival_location_id=arrival.id,
        scheduled_departure=flight.scheduled_departure,
        scheduled_arrival=flight.scheduled_arrival,
        estimated_arrival=flight.estimated_arrival,
        status=flight.status,
        source=TripSource.PROVIDER,
        provider_ref=flight.provider_ref,
        next_poll_at=datetime.now(UTC),  # опрос в воркере подхватит рейс сразу
    )
    session.add(trip)
    await session.flush()
    return trip


async def search_flights(
    session: AsyncSession,
    event_id: uuid.UUID,
    user: User,
    number: str,
    departure_date: date,
    provider: FlightProvider | None,
) -> list[Trip]:
    """Поиск у поставщика и сохранение найденных рейсов в группе (в порядке ответа поставщика).

    Доступ — участнику и админу группы, иначе 404. Поставщик не настроен, недоступен или не
    ответил за `PROVIDER_TIMEOUT_SECONDS` — 503. Рейс с аэропортом вне справочника пропускается.
    """
    event = await get_event_for_member(session, event_id, user)
    flights = await _query_provider(provider, normalize_number(number), departure_date)
    airports = await _airports(session, flights)

    await lock_event(session, event.id)
    saved: list[uuid.UUID] = []
    for flight in flights:
        departure = airports.get(flight.departure_iata)
        arrival = airports.get(flight.arrival_iata)
        if departure is None or arrival is None:
            logger.warning(
                "Рейс поставщика пропущен: аэропорта нет в справочнике (%s -> %s)",
                flight.departure_iata,
                flight.arrival_iata,
            )
            continue
        if flight.scheduled_arrival <= flight.scheduled_departure:
            logger.warning(
                "Рейс поставщика пропущен: прилёт не позже вылета (%s -> %s)",
                flight.departure_iata,
                flight.arrival_iata,
            )
            continue
        saved.append((await _save(session, event.id, flight, departure, arrival)).id)
    await session.commit()

    stmt = (
        select(Trip)
        .where(Trip.id.in_(saved))
        .options(selectinload(Trip.departure_location), selectinload(Trip.arrival_location))
        .execution_options(populate_existing=True)
    )
    by_id = {trip.id: trip for trip in await session.scalars(stmt)}
    return [by_id[trip_id] for trip_id in saved]
