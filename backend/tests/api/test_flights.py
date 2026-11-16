import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_flight_provider
from app.flights import FakeFlightProvider, FlightProvider, ProviderFlight
from app.models import BackgroundJob, Event, Trip, TripSource, TripStatus, User
from app.services import flights as flights_service
from tests.api.conftest import auth_headers
from tests.factories import get_location, make_event, make_membership, make_trip
from tests.helpers.contract import assert_contract

DAY = date(2026, 11, 12)
DEPARTURE = datetime(2026, 11, 12, 7, 0, tzinfo=UTC)


def provider_flight(**overrides: Any) -> ProviderFlight:
    fields: dict[str, Any] = {
        "provider_ref": "ref-1",
        "number": "SU1234",
        "departure_iata": "OVB",
        "arrival_iata": "SVO",
        "scheduled_departure": DEPARTURE,
        "scheduled_arrival": DEPARTURE + timedelta(hours=4),
        "estimated_arrival": DEPARTURE + timedelta(hours=4, minutes=20),
        "status": TripStatus.DELAYED,
    }
    fields.update(overrides)
    return ProviderFlight(**fields)


@pytest.fixture
def fake(app: FastAPI) -> FakeFlightProvider:
    provider = FakeFlightProvider([provider_flight()])
    app.dependency_overrides[get_flight_provider] = lambda: provider
    return provider


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    return await make_event(db_session, admin_id=user.id)


async def search(
    client: AsyncClient,
    event_id: Any,
    number: str = "su 1234",
    day: str = "2026-11-12",
    telegram_id: int | None = 42,
) -> Response:
    headers = {} if telegram_id is None else auth_headers(telegram_id)
    response = await client.get(
        "/flights/search",
        params={"event_id": str(event_id), "number": number, "date": day},
        headers=headers,
    )
    assert_contract(response, "GET", "/flights/search")
    return response


async def trips_in(session: AsyncSession, event_id: uuid.UUID) -> list[Trip]:
    return list((await session.scalars(select(Trip).where(Trip.event_id == event_id))).all())


async def test_search_saves_provider_flight(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, fake: FakeFlightProvider
) -> None:
    response = await search(api_client, event.id)

    assert response.status_code == 200
    [body] = response.json()
    assert body["number"] == "SU1234"
    assert body["source"] == "provider"
    assert body["status"] == "delayed"
    assert body["departure_location"]["code"] == "OVB"
    assert body["arrival_location"]["code"] == "SVO"
    assert body["scheduled_departure"] == "2026-11-12T14:00:00+07:00"
    assert body["estimated_arrival"] == "2026-11-12T14:20:00+03:00"
    [stored] = await trips_in(db_session, event.id)
    assert str(stored.id) == body["id"]
    assert stored.source == TripSource.PROVIDER
    assert stored.provider_ref == "ref-1"
    assert stored.next_poll_at is not None
    assert abs(stored.next_poll_at - datetime.now(UTC)) < timedelta(minutes=1)


async def test_repeated_search_does_not_duplicate(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, fake: FakeFlightProvider
) -> None:
    first = (await search(api_client, event.id)).json()
    fake.add(provider_flight(status=TripStatus.CANCELLED))  # данные меняются только опросом (T8)

    second = (await search(api_client, event.id)).json()

    assert [t["id"] for t in second] == [t["id"] for t in first]
    assert second[0]["status"] == "delayed"
    assert len(await trips_in(db_session, event.id)) == 1


async def test_manual_trip_with_same_key_is_adopted(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, fake: FakeFlightProvider
) -> None:
    manual = await make_trip(
        db_session,
        event_id=event.id,
        number="SU1234",
        departure_location_id=(await get_location(db_session, "OVB")).id,
        arrival_location_id=(await get_location(db_session, "LED")).id,
        scheduled_departure=DEPARTURE,
        scheduled_arrival=DEPARTURE + timedelta(hours=5),
    )
    await db_session.commit()

    response = await search(api_client, event.id)

    [body] = response.json()
    assert body["id"] == str(manual.id)
    assert body["source"] == "provider"
    assert body["arrival_location"]["code"] == "SVO"
    [stored] = await trips_in(db_session, event.id)
    await db_session.refresh(stored)
    assert stored.provider_ref == "ref-1"
    assert stored.status == TripStatus.DELAYED
    assert stored.estimated_arrival == DEPARTURE + timedelta(hours=4, minutes=20)
    jobs = await db_session.scalar(
        select(func.count()).select_from(BackgroundJob).where(BackgroundJob.event_id == event.id)
    )
    assert jobs == 1


async def test_unknown_airport_skips_flight_with_warning(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    fake: FakeFlightProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.add(provider_flight(provider_ref="ref-2", arrival_iata="ZZZ"))

    with caplog.at_level(logging.WARNING, logger="app.services.flights"):
        response = await search(api_client, event.id)

    assert [t["source"] for t in response.json()] == ["provider"]
    assert len(response.json()) == 1
    assert len(await trips_in(db_session, event.id)) == 1
    [record] = [r for r in caplog.records if r.name == "app.services.flights"]
    assert "ZZZ" in record.getMessage()
    assert "SU1234" not in record.getMessage()


async def test_flight_with_arrival_before_departure_is_skipped(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider
) -> None:
    fake.add(provider_flight(provider_ref="ref-2", scheduled_arrival=DEPARTURE))

    response = await search(api_client, event.id)

    assert len(response.json()) == 1


async def test_no_provider_configured_is_503(
    api_client: AsyncClient, app: FastAPI, event: Event
) -> None:
    app.dependency_overrides[get_flight_provider] = lambda: None

    response = await search(api_client, event.id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FLIGHT_PROVIDER_UNAVAILABLE"


async def test_unavailable_provider_is_503(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider
) -> None:
    fake.unavailable = True

    response = await search(api_client, event.id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FLIGHT_PROVIDER_UNAVAILABLE"


async def test_provider_timeout_is_503(
    api_client: AsyncClient,
    event: Event,
    fake: FakeFlightProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flights_service, "PROVIDER_TIMEOUT_SECONDS", 0.05)
    fake.delay = 1.0

    response = await search(api_client, event.id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FLIGHT_PROVIDER_UNAVAILABLE"


async def test_stranger_gets_404(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    other_user: User,
    fake: FakeFlightProvider,
) -> None:
    response = await search(api_client, event.id, telegram_id=43)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert await trips_in(db_session, event.id) == []


async def test_unknown_event_gets_404(api_client: AsyncClient, fake: FakeFlightProvider) -> None:
    response = await search(api_client, uuid.uuid4())

    assert response.status_code == 404


async def test_unauthorized_is_401(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider
) -> None:
    response = await search(api_client, event.id, telegram_id=None)

    assert response.status_code == 401


async def test_member_can_search(
    api_client: AsyncClient,
    db_session: AsyncSession,
    other_user: User,
    fake: FakeFlightProvider,
) -> None:
    admin = await db_session.get(User, other_user.id)
    assert admin is not None
    event = await make_event(db_session, admin_id=admin.id)
    from tests.factories import make_user

    member = await make_user(db_session, telegram_id=42, first_name="Ivan")
    await make_membership(db_session, event_id=event.id, user_id=member.id)
    await db_session.commit()

    response = await search(api_client, event.id, telegram_id=42)

    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_admin_without_membership_can_search(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider
) -> None:
    response = await search(api_client, event.id)

    assert response.status_code == 200


@pytest.mark.parametrize("number", ["S", "SU", "SU-1234", "SU1234567", "SU 12 34"])
async def test_bad_number_is_422(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider, number: str
) -> None:
    response = await search(api_client, event.id, number=number)

    assert response.status_code == 422


@pytest.mark.parametrize("day", ["12.11.2026", "2026-13-01", "tomorrow"])
async def test_bad_date_is_422(
    api_client: AsyncClient, event: Event, fake: FakeFlightProvider, day: str
) -> None:
    response = await search(api_client, event.id, day=day)

    assert response.status_code == 422


async def test_missing_params_is_422(api_client: AsyncClient, fake: FakeFlightProvider) -> None:
    response = await api_client.get("/flights/search", headers=auth_headers())
    assert_contract(response, "GET", "/flights/search")

    assert response.status_code == 422


async def test_fake_provider_setting_generates_flight(
    api_client: AsyncClient, app: FastAPI, event: Event
) -> None:
    provider: FlightProvider = FakeFlightProvider()
    app.dependency_overrides[get_flight_provider] = lambda: provider

    response = await search(api_client, event.id, number="DP 401")

    [body] = response.json()
    assert body["number"] == "DP401"
    assert body["departure_location"]["code"] == "SVO"
    assert body["arrival_location"]["code"] == "LED"
    assert body["scheduled_departure"] == "2026-11-12T10:00:00+03:00"


@pytest.fixture(autouse=True)
def _clear_overrides(app: FastAPI) -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_flight_provider, None)
