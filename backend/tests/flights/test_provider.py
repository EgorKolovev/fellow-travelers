from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.flights import (
    FakeFlightProvider,
    ProviderFlight,
    ProviderUnavailableError,
    build_provider,
)
from app.models.enums import TripStatus

DEPARTURE = datetime(2026, 11, 12, 7, 0, tzinfo=UTC)


def flight(**overrides: object) -> ProviderFlight:
    fields: dict[str, object] = {
        "provider_ref": "ref-1",
        "number": "SU1234",
        "departure_iata": "OVB",
        "arrival_iata": "SVO",
        "scheduled_departure": DEPARTURE,
        "scheduled_arrival": DEPARTURE + timedelta(hours=4),
        "estimated_arrival": None,
        "status": TripStatus.SCHEDULED,
    }
    fields.update(overrides)
    return ProviderFlight(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["scheduled_departure", "scheduled_arrival", "estimated_arrival"])
def test_provider_flight_rejects_naive_time(field: str) -> None:
    with pytest.raises(ValueError, match="часовым поясом"):
        flight(**{field: datetime(2026, 11, 12, 10, 0)})


def test_provider_flight_is_frozen() -> None:
    with pytest.raises(AttributeError):
        flight().number = "SU1"  # type: ignore[misc]


async def test_fake_returns_added_flights_matching_number_and_date() -> None:
    provider = FakeFlightProvider([flight()])
    provider.add(flight(provider_ref="ref-2", number="SU9999"))

    found = await provider.search("su 1234", date(2026, 11, 12))

    assert [f.provider_ref for f in found] == ["ref-1"]
    assert await provider.search("SU1234", date(2026, 11, 13)) == []
    assert await provider.search("SU7777", date(2026, 11, 12)) == []


async def test_fake_fetch_returns_added_flight_or_none() -> None:
    provider = FakeFlightProvider([flight()])

    assert (await provider.fetch("ref-1")) == flight()
    assert await provider.fetch("missing") is None


async def test_fake_generates_deterministic_flight_without_added_flights() -> None:
    provider = FakeFlightProvider()

    first = await provider.search("su 1234", date(2026, 11, 12))
    second = await provider.search("SU1234", date(2026, 11, 12))

    assert first == second
    assert len(first) == 1
    generated = first[0]
    assert generated.number == "SU1234"
    assert (generated.departure_iata, generated.arrival_iata) == ("SVO", "LED")
    assert generated.scheduled_departure == datetime(2026, 11, 12, 7, 0, tzinfo=UTC)  # 10:00 MSK
    assert generated.scheduled_arrival == generated.scheduled_departure + timedelta(hours=2)
    assert generated.status == TripStatus.SCHEDULED
    assert (await provider.search("SU1234", date(2026, 11, 13)))[0] != generated
    assert await provider.fetch(generated.provider_ref) == generated


async def test_fake_fetch_restores_generated_flight_in_another_instance() -> None:
    found = (await FakeFlightProvider().search("su 1234", date(2026, 11, 12)))[0]
    worker_side = FakeFlightProvider()  # отдельный процесс: поиска он не видел

    assert await worker_side.fetch(found.provider_ref) == found
    assert await worker_side.fetch("fake:SU1234:not-a-date") is None
    assert await worker_side.fetch("fake::2026-11-12") is None
    assert await worker_side.fetch("other:SU1234:2026-11-12") is None
    assert await worker_side.fetch("ref-1") is None


async def test_fake_generation_does_not_accumulate_flights() -> None:
    provider = FakeFlightProvider()

    for day in range(1, 29):
        await provider.search("SU1234", date(2026, 11, day))

    assert provider._flights == {}


async def test_fake_unavailable_raises_in_both_methods() -> None:
    provider = FakeFlightProvider([flight()], unavailable=True)

    with pytest.raises(ProviderUnavailableError):
        await provider.search("SU1234", date(2026, 11, 12))
    with pytest.raises(ProviderUnavailableError):
        await provider.fetch("ref-1")
    provider.unavailable = False
    assert await provider.fetch("ref-1") is not None


async def test_fake_delay_slows_response() -> None:
    provider = FakeFlightProvider(delay=0.05)

    started = datetime.now(UTC)
    await provider.search("SU1234", date(2026, 11, 12))

    assert datetime.now(UTC) - started >= timedelta(milliseconds=45)


def _settings(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")
    if value is None:
        monkeypatch.delenv("FLIGHT_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("FLIGHT_PROVIDER", value)
    return Settings(_env_file=None)


def test_build_provider_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_provider(_settings(monkeypatch, None)) is None
    assert build_provider(_settings(monkeypatch, "none")) is None


def test_build_provider_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_provider(_settings(monkeypatch, "fake")), FakeFlightProvider)


def test_unknown_provider_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="flight_provider"):
        _settings(monkeypatch, "aviationstack")
