import asyncio
import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, ErrorCode
from app.flights.provider import FlightProvider, ProviderFlight, ProviderUnavailableError
from app.models import Location, LocationKind, TransportMode, Trip, TripSource, User
from app.schemas.trip import normalize_number
from app.services.access import get_event_for_member
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


async def _save(
    session: AsyncSession,
    event_id: uuid.UUID,
    flight: ProviderFlight,
    departure: Location,
    arrival: Location,
) -> Trip:
    """Рейс поставщика в группе: уже сохранённый не трогаем, ручной усыновляем, иначе создаём."""
    existing = await session.scalar(
        select(Trip).where(Trip.event_id == event_id, Trip.provider_ref == flight.provider_ref)
    )
    if existing is not None:
        return existing  # обновление данных рейса — задача опроса в воркере
    manual = await session.scalar(
        select(Trip).where(
            Trip.event_id == event_id,
            Trip.mode == TransportMode.FLIGHT,
            Trip.number == flight.number,
            Trip.scheduled_departure == flight.scheduled_departure,
        )
    )
    now = datetime.now(UTC)
    if manual is None:
        trip = Trip(event_id=event_id, mode=TransportMode.FLIGHT)
        session.add(trip)
    else:
        trip = manual
    arrival_before = trip.effective_arrival if manual is not None else None
    arrival_location_before = trip.arrival_location_id if manual is not None else None
    trip.number = flight.number
    trip.departure_location_id = departure.id
    trip.arrival_location_id = arrival.id
    trip.scheduled_departure = flight.scheduled_departure
    trip.scheduled_arrival = flight.scheduled_arrival
    trip.estimated_arrival = flight.estimated_arrival
    trip.status = flight.status
    trip.source = TripSource.PROVIDER
    trip.provider_ref = flight.provider_ref
    trip.next_poll_at = now  # опрос в воркере подхватит рейс сразу
    await session.flush()
    if manual is not None and (
        trip.effective_arrival != arrival_before or arrival.id != arrival_location_before
    ):
        await enqueue_reconcile(session, event_id=event_id, trip_id=trip.id)
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
