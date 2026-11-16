"""Опрос поставщика рейсов в воркере (Д2, Д3): аренда, запрос вне транзакции, применение."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.flights import polling
from app.flights.provider import FlightProvider, ProviderFlight, ProviderUnavailableError
from app.models import Application, ApplicationStatus, Trip
from app.observability import metrics
from app.observability.deferred import defer_increment
from app.observability.metrics import FlightPollOutcome
from app.services.flights import apply_update
from app.services.matching import lock_event

logger = logging.getLogger(__name__)

# сколько ждём ответ поставщика; константа модуля, чтобы тест мог её уменьшить
PROVIDER_TIMEOUT_SECONDS = 10.0
# аренда: пока рейс опрашивают, другой воркер его не берёт; если воркер упал, рейс вернётся сам
LEASE = timedelta(minutes=5)
UNAVAILABLE_RETRY = timedelta(minutes=10)
ERROR_RETRY = timedelta(minutes=10)

_ACTIVE_STATUSES = (ApplicationStatus.SEARCHING, ApplicationStatus.ASSIGNED, ApplicationStatus.SOLO)


@dataclass(frozen=True, slots=True)
class PollResult:
    """Итог прохода: взято рейсов, из них изменилось, не найдено у поставщика, не ответил."""

    polled: int = 0
    changed: int = 0
    missing: int = 0
    unavailable: int = 0


@dataclass(frozen=True, slots=True)
class _Leased:
    trip_id: uuid.UUID
    event_id: uuid.UUID
    provider_ref: str


async def _lease_due(
    session_factory: async_sessionmaker[AsyncSession], now: datetime, batch: int
) -> list[_Leased]:
    """Берёт до `batch` рейсов, которым пора, и отодвигает им `next_poll_at` на `LEASE`.

    Рейс поставщика берётся, если есть заявка на него в `searching`, `assigned` или `solo`
    без «Уехал» (Д3). `FOR UPDATE SKIP LOCKED` делит рейсы между параллельными воркерами.
    """
    async with session_factory() as session:
        trips = (
            await session.execute(
                select(Trip.id, Trip.event_id, Trip.provider_ref)
                .where(
                    Trip.provider_ref.is_not(None),
                    Trip.next_poll_at <= now,
                    exists().where(
                        Application.trip_id == Trip.id,
                        Application.status.in_(_ACTIVE_STATUSES),
                        Application.departed_at.is_(None),
                    ),
                )
                .order_by(Trip.next_poll_at, Trip.id)
                .limit(batch)
                .with_for_update(of=Trip, skip_locked=True)
            )
        ).all()
        if not trips:
            return []
        await session.execute(
            update(Trip)
            .where(Trip.id.in_([row.id for row in trips]))
            .values(next_poll_at=now + LEASE)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return [_Leased(row.id, row.event_id, str(row.provider_ref)) for row in trips]


async def _store_retry(
    session_factory: async_sessionmaker[AsyncSession],
    leased: _Leased,
    now: datetime,
    *,
    pause: timedelta,
    outcome: FlightPollOutcome | None = None,
) -> None:
    """Повтор через `pause`, но не позже расписания Д2; если расписание кончилось — `None`."""
    async with session_factory() as session:
        trip = await session.scalar(select(Trip).where(Trip.id == leased.trip_id))
        if trip is not None:
            scheduled = polling.next_poll_at(
                scheduled_departure=trip.scheduled_departure,
                effective_arrival=trip.effective_arrival,
                status=trip.status,
                now=now,
            )
            trip.next_poll_at = None if scheduled is None else min(scheduled, now + pause)
        if outcome is not None:
            defer_increment(session, metrics.flight_polls_total.labels(outcome))
        await session.commit()


async def _store_answer(
    session_factory: async_sessionmaker[AsyncSession],
    leased: _Leased,
    flight: ProviderFlight | None,
    now: datetime,
) -> FlightPollOutcome:
    """Применяет ответ поставщика в своей транзакции под `lock_event` группы."""
    async with session_factory() as session:
        await lock_event(session, leased.event_id)
        trip = await session.scalar(
            select(Trip).where(Trip.id == leased.trip_id).execution_options(populate_existing=True)
        )
        if trip is None or trip.provider_ref != leased.provider_ref:
            return FlightPollOutcome.UNCHANGED  # рейс удалён или отвязан, пока шёл запрос
        if flight is None:
            logger.warning("Поставщик не знает рейс", extra={"trip_id": str(trip.id)})
            outcome = FlightPollOutcome.MISSING
        elif await apply_update(session, trip, flight):
            outcome = FlightPollOutcome.CHANGED
        else:
            outcome = FlightPollOutcome.UNCHANGED
        trip.last_polled_at = now
        trip.next_poll_at = polling.next_poll_at(
            scheduled_departure=trip.scheduled_departure,
            effective_arrival=trip.effective_arrival,
            status=trip.status,
            now=now,
        )
        defer_increment(session, metrics.flight_polls_total.labels(outcome))
        await session.commit()
    return outcome


async def poll_due_trips(
    session_factory: async_sessionmaker[AsyncSession],
    provider: FlightProvider,
    *,
    now: datetime | None = None,
    batch: int = 20,
    stop: asyncio.Event | None = None,
) -> PollResult:
    """Один проход опроса: рейсы, которым пора, спрашиваются у поставщика и обновляются.

    `polled` — сколько рейсов взято в этом проходе. Ошибка одного рейса не останавливает
    остальные: она пишется в лог (имя класса исключения), рейс получает паузу `ERROR_RETRY`
    (не позже расписания Д2; если расписание закончилось, опрос прекращается). Если задан
    `stop`, после него новые рейсы не начинаются.
    """
    now = now or datetime.now(UTC)
    leased = await _lease_due(session_factory, now, batch)
    changed = missing = unavailable = polled = 0
    for item in leased:
        if stop is not None and stop.is_set():
            break
        polled += 1
        try:
            try:
                async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
                    flight = await provider.fetch(item.provider_ref)
            except (ProviderUnavailableError, TimeoutError):
                logger.warning("Поставщик рейсов не ответил", extra={"trip_id": str(item.trip_id)})
                await _store_retry(
                    session_factory,
                    item,
                    now,
                    pause=UNAVAILABLE_RETRY,
                    outcome=FlightPollOutcome.UNAVAILABLE,
                )
                unavailable += 1
                continue
            outcome = await _store_answer(session_factory, item, flight, now)
        except Exception as exc:
            logger.error(
                "Опрос рейса не удался (%s)",
                type(exc).__name__,
                extra={"trip_id": str(item.trip_id)},
            )
            metrics.flight_polls_total.labels(FlightPollOutcome.ERROR).inc()
            try:
                await _store_retry(session_factory, item, now, pause=ERROR_RETRY)
            except Exception as retry_exc:  # аренда истечёт сама и вернёт рейс в опрос
                logger.error(
                    "Пауза после ошибки опроса не записана (%s)",
                    type(retry_exc).__name__,
                    extra={"trip_id": str(item.trip_id)},
                )
            continue
        if outcome == FlightPollOutcome.CHANGED:
            changed += 1
        elif outcome == FlightPollOutcome.MISSING:
            missing += 1
    return PollResult(polled=polled, changed=changed, missing=missing, unavailable=unavailable)
