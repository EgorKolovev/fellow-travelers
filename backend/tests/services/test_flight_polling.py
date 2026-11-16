"""`poll_due_trips`: выборка, аренда, обновление рейсов поставщика (Д2, Д3).

Сессии открываются на `db_engine` с commit: аренда и параллельные проходы на одной транзакции
`db_session` не проверить. Фикстура `group` удаляет данные после теста.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.flights import FakeFlightProvider, ProviderFlight
from app.models import (
    ApplicationStatus,
    BackgroundJob,
    Event,
    Trip,
    TripSource,
    TripStatus,
    User,
)
from app.services import flight_polling
from app.services.flight_polling import LEASE, UNAVAILABLE_RETRY, PollResult, poll_due_trips
from tests.factories import make_application, make_event, make_trip, make_user
from tests.helpers.audit_replay import audit_rows
from tests.helpers.concurrency import wait

NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
DEP = NOW + timedelta(hours=30)  # до вылета больше суток: следующий опрос через 60 мин
ARR = DEP + timedelta(hours=3)


class CountingProvider(FakeFlightProvider):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fetched: list[str] = []

    async def fetch(self, provider_ref: str) -> ProviderFlight | None:
        self.fetched.append(provider_ref)
        return await super().fetch(provider_ref)


@dataclass
class Group:
    event_id: uuid.UUID
    admin_id: uuid.UUID
    user_ids: list[uuid.UUID]


def _flight(ref: str, number: str, **overrides: object) -> ProviderFlight:
    fields: dict[str, object] = {
        "provider_ref": ref,
        "number": number,
        "departure_iata": "SVX",
        "arrival_iata": "SVO",
        "scheduled_departure": DEP,
        "scheduled_arrival": ARR,
        "estimated_arrival": None,
        "status": TripStatus.SCHEDULED,
    }
    fields.update(overrides)
    return ProviderFlight(**fields)  # type: ignore[arg-type]


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def group(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Group]:
    async with sessions() as session:
        admin = await make_user(session)
        event = await make_event(session, admin_id=admin.id)
        await session.commit()
        created = Group(event_id=event.id, admin_id=admin.id, user_ids=[])
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(
                delete(User).where(User.id.in_([created.admin_id, *created.user_ids]))
            )
            await session.commit()


async def _trip(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    number: str,
    *,
    ref: str | None = None,
    due: bool = True,
    application: ApplicationStatus | None = ApplicationStatus.SEARCHING,
    departed: bool = False,
    **kw: object,
) -> uuid.UUID:
    async with sessions() as session:
        trip = await make_trip(
            session,
            event_id=group.event_id,
            number=number,
            scheduled_departure=DEP,
            scheduled_arrival=ARR,
            provider_ref=ref or f"ref-{number}",
            source=TripSource.PROVIDER,
            status=TripStatus.SCHEDULED,
            next_poll_at=NOW - timedelta(minutes=1) if due else NOW + timedelta(hours=1),
            **kw,
        )
        if application is not None:
            user = await make_user(session)
            group.user_ids.append(user.id)
            await make_application(
                session,
                event_id=group.event_id,
                trip_id=trip.id,
                user_id=user.id,
                status=application,
                departed_at=NOW if departed else None,
            )
        await session.commit()
        return trip.id


async def _load(sessions: async_sessionmaker[AsyncSession], trip_id: uuid.UUID) -> Trip:
    async with sessions() as session:
        return await session.scalar(select(Trip).where(Trip.id == trip_id))  # type: ignore[return-value]


def _counter(outcome: str) -> float:
    return REGISTRY.get_sample_value("ft_flight_polls_total", {"outcome": outcome}) or 0.0


async def test_polls_only_due_trips_with_active_applications(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _trip(sessions, group, "SU0001")
    await _trip(sessions, group, "SU0002", application=ApplicationStatus.SOLO)
    await _trip(sessions, group, "SU0003", due=False)
    await _trip(sessions, group, "SU0004", application=None)
    await _trip(sessions, group, "SU0005", application=ApplicationStatus.SOLO, departed=True)
    await _trip(sessions, group, "SU0006", application=ApplicationStatus.CANCELLED)
    manual = await _trip(sessions, group, "SU0007", ref="x")
    async with sessions() as session:  # ручной рейс: без provider_ref
        trip = await session.get(Trip, manual)
        assert trip is not None
        trip.provider_ref = None
        await session.commit()
    provider = CountingProvider([_flight(f"ref-SU000{i}", f"SU000{i}") for i in range(1, 7)])

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result == PollResult(polled=2, changed=0, missing=0, unavailable=0)
    assert sorted(provider.fetched) == ["ref-SU0001", "ref-SU0002"]


async def test_estimate_shift_updates_trip_and_schedules_next_poll(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    estimate = ARR + timedelta(minutes=45)
    provider = CountingProvider([_flight("ref-SU0001", "SU0001", estimated_arrival=estimate)])
    changed_before = _counter("changed")

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result == PollResult(polled=1, changed=1)
    trip = await _load(sessions, trip_id)
    assert trip.estimated_arrival == estimate
    assert trip.last_polled_at == NOW
    assert trip.next_poll_at == NOW + timedelta(minutes=60)  # Д2: больше суток до вылета
    async with sessions() as session:
        (row,) = await audit_rows(session, group.event_id)
        assert (row.action, row.reason, row.actor_kind) == (
            "trip_updated",
            "provider_update",
            "system",
        )
        jobs = await session.scalar(
            select(func.count())
            .select_from(BackgroundJob)
            .where(BackgroundJob.event_id == group.event_id)
        )
        assert jobs == 1
    assert _counter("changed") == changed_before + 1


async def test_repeat_without_changes_writes_no_history(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    provider = CountingProvider(
        [_flight("ref-SU0001", "SU0001", estimated_arrival=ARR + timedelta(minutes=45))]
    )
    await poll_due_trips(sessions, provider, now=NOW)
    unchanged_before = _counter("unchanged")

    later = NOW + timedelta(minutes=61)
    result = await poll_due_trips(sessions, provider, now=later)

    assert result == PollResult(polled=1)
    assert _counter("unchanged") == unchanged_before + 1
    async with sessions() as session:
        assert len(await audit_rows(session, group.event_id)) == 1
    trip = await _load(sessions, trip_id)
    assert trip.last_polled_at == later
    assert trip.next_poll_at == later + timedelta(minutes=60)


async def test_not_due_again_right_after_poll(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _trip(sessions, group, "SU0001")
    provider = CountingProvider([_flight("ref-SU0001", "SU0001")])

    await poll_due_trips(sessions, provider, now=NOW)
    again = await poll_due_trips(sessions, provider, now=NOW + timedelta(minutes=1))

    assert again == PollResult()
    assert provider.fetched == ["ref-SU0001"]


async def test_unavailable_provider_retries_in_ten_minutes(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    provider = CountingProvider(
        [_flight("ref-SU0001", "SU0001", estimated_arrival=ARR + timedelta(hours=1))],
        unavailable=True,
    )
    caplog.set_level(logging.WARNING)
    before = _counter("unavailable")

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result == PollResult(polled=1, unavailable=1)
    trip = await _load(sessions, trip_id)
    assert trip.next_poll_at == NOW + UNAVAILABLE_RETRY
    assert trip.estimated_arrival is None
    assert trip.last_polled_at is None
    assert _counter("unavailable") == before + 1
    assert all("SU0001" not in r.getMessage() for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_timeout_counts_as_unavailable(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    monkeypatch.setattr(flight_polling, "PROVIDER_TIMEOUT_SECONDS", 0.05)
    provider = CountingProvider([_flight("ref-SU0001", "SU0001")], delay=1.0)

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result == PollResult(polled=1, unavailable=1)
    assert (await _load(sessions, trip_id)).next_poll_at == NOW + UNAVAILABLE_RETRY


async def test_unknown_to_provider_follows_schedule_and_warns(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    provider = CountingProvider([_flight("other", "SU0099")])
    caplog.set_level(logging.WARNING)

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result == PollResult(polled=1, missing=1)
    trip = await _load(sessions, trip_id)
    assert trip.next_poll_at == NOW + timedelta(minutes=60)
    assert trip.last_polled_at == NOW
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert all("SU0001" not in r.getMessage() for r in caplog.records)


async def test_cancelled_flight_stops_polling(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    trip_id = await _trip(sessions, group, "SU0001")
    provider = CountingProvider([_flight("ref-SU0001", "SU0001", status=TripStatus.CANCELLED)])

    await poll_due_trips(sessions, provider, now=NOW)

    trip = await _load(sessions, trip_id)
    assert trip.status == TripStatus.CANCELLED
    assert trip.next_poll_at is None


async def test_error_in_one_trip_does_not_stop_others(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = await _trip(sessions, group, "SU0001")
    good = await _trip(sessions, group, "SU0002")
    # первый рейс при применении нарушит уникальность (номер и вылет уже заняты вторым)
    provider = CountingProvider(
        [
            _flight("ref-SU0001", "SU0002"),
            _flight("ref-SU0002", "SU0002", estimated_arrival=ARR + timedelta(minutes=30)),
        ]
    )
    caplog.set_level(logging.ERROR)
    before = _counter("error")

    result = await poll_due_trips(sessions, provider, now=NOW)

    assert result.polled == 2
    assert _counter("error") == before + 1
    assert (await _load(sessions, good)).estimated_arrival == ARR + timedelta(minutes=30)
    bad_trip = await _load(sessions, bad)
    assert bad_trip.next_poll_at == NOW + LEASE  # аренда истечёт, и рейс опросят снова
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "IntegrityError" in record.getMessage()
    assert "SU000" not in record.getMessage()


async def test_batch_limits_and_orders_by_next_poll(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    for i in range(3):
        await _trip(sessions, group, f"SU000{i}")
    async with sessions() as session:
        for i, offset in enumerate((3, 1, 2)):
            await session.execute(
                update(Trip)
                .where(Trip.number == f"SU000{i}", Trip.event_id == group.event_id)
                .values(next_poll_at=NOW - timedelta(minutes=offset))
            )
        await session.commit()
    provider = CountingProvider([_flight(f"ref-SU000{i}", f"SU000{i}") for i in range(3)])

    result = await poll_due_trips(sessions, provider, now=NOW, batch=2)

    assert result.polled == 2
    assert provider.fetched == ["ref-SU0000", "ref-SU0002"]  # самые давние


async def test_parallel_passes_do_not_poll_same_trip_twice(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    for i in range(4):
        await _trip(sessions, group, f"SU000{i}")
    provider = CountingProvider(
        [_flight(f"ref-SU000{i}", f"SU000{i}") for i in range(4)], delay=0.2
    )

    results = await wait(
        asyncio.gather(
            poll_due_trips(sessions, provider, now=NOW, batch=2),
            poll_due_trips(sessions, provider, now=NOW, batch=2),
        )
    )

    assert sorted(provider.fetched) == [f"ref-SU000{i}" for i in range(4)]
    assert [r.polled for r in results] == [2, 2]
