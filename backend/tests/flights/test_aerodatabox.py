"""Адаптер AeroDataBox (M-01a): запись примеров по официальной схеме, без сети."""

import copy
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.flights import ProviderFlight, ProviderUnavailableError
from app.flights.aerodatabox import AeroDataBoxProvider
from app.models import ApplicationStatus, Event, Trip, TripSource, TripStatus, User
from app.services.flight_polling import poll_due_trips
from app.services.flights import search_flights
from tests.factories import make_application, make_event, make_user

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "sekret-key-0123456789"
DAY = date(2026, 11, 20)
DEPARTURE = datetime(2026, 11, 20, 7, 20, tzinfo=UTC)
ARRIVAL = datetime(2026, 11, 20, 8, 50, tzinfo=UTC)
Gateway = Literal["direct", "apimarket", "rapidapi"]


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class FakeHttp:
    """Внедряемая функция запроса: записывает вызовы, отвечает заданным кодом и телом."""

    def __init__(
        self, status: int = 200, body: Any = None, error: BaseException | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    async def __call__(
        self, url: str, headers: dict[str, str], params: dict[str, str]
    ) -> tuple[int, Any]:
        self.calls.append((url, headers, params))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def provider_with(
    body: Any = None,
    status: int = 200,
    *,
    gateway: Gateway = "apimarket",
    error: BaseException | None = None,
) -> tuple[AeroDataBoxProvider, FakeHttp]:
    http = FakeHttp(status, body, error)
    return AeroDataBoxProvider(KEY, gateway, http=http), http


def entry(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(load("su1120_scheduled")[0])
    data.update(overrides)
    return data


# --- шлюзы, URL, заголовки ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gateway", "base", "headers"),
    [
        ("direct", "https://api.aerodatabox.com", {"X-Api-Key": KEY}),
        (
            "apimarket",
            "https://prod.api.market/api/v1/aedbx/aerodatabox",
            {"x-api-market-key": KEY},
        ),
        (
            "rapidapi",
            "https://aerodatabox.p.rapidapi.com",
            {"X-RapidAPI-Key": KEY, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"},
        ),
    ],
)
async def test_gateway_url_headers_and_params(
    gateway: Gateway, base: str, headers: dict[str, str]
) -> None:
    provider, http = provider_with([], 204, gateway=gateway)

    await provider.search("SU1120", DAY)

    assert len(http.calls) == 1
    url, sent_headers, params = http.calls[0]
    assert url == f"{base}/flights/Number/SU1120/2026-11-20"
    assert sent_headers == headers
    assert params == {
        "dateLocalRole": "Departure",
        "withAircraftImage": "false",
        "withLocation": "false",
    }


async def test_number_is_normalized_in_url() -> None:
    provider, http = provider_with(None, 204)

    await provider.search(" su 1120 ", DAY)

    assert http.calls[0][0].endswith("/flights/Number/SU1120/2026-11-20")


async def test_path_segments_are_quoted() -> None:
    provider, http = provider_with(None, 204)

    await provider.search("s/u?1#", DAY)

    assert http.calls[0][0].endswith("/flights/Number/S%2FU%3F1%23/2026-11-20")


def test_unknown_gateway_is_rejected() -> None:
    with pytest.raises(ValueError, match="gateway"):
        AeroDataBoxProvider(KEY, "nope")  # type: ignore[arg-type]


def test_repr_hides_the_key() -> None:
    assert KEY not in repr(AeroDataBoxProvider(KEY, "direct"))


# --- разбор ответа -----------------------------------------------------------------------------


async def test_parses_regular_flight() -> None:
    provider, _ = provider_with(load("su1120_scheduled"))

    [flight] = await provider.search("SU1120", DAY)

    assert flight == ProviderFlight(
        provider_ref="adb:SU1120:2026-11-20:SVO",
        number="SU1120",
        departure_iata="SVO",
        arrival_iata="LED",
        scheduled_departure=DEPARTURE,
        scheduled_arrival=ARRIVAL,
        estimated_arrival=None,
        status=TripStatus.SCHEDULED,
    )
    assert flight.scheduled_departure.utcoffset() == timedelta(0)


async def test_parses_delayed_flight() -> None:
    provider, _ = provider_with(load("su1120_delayed"))

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == TripStatus.DELAYED
    assert flight.estimated_arrival == ARRIVAL + timedelta(minutes=40)
    assert flight.scheduled_arrival == ARRIVAL


@pytest.mark.parametrize(
    ("fixture", "status"),
    [
        ("su1120_arrived", TripStatus.LANDED),
        ("su1120_canceled", TripStatus.CANCELLED),
        ("su1120_canceled_uncertain", TripStatus.UNKNOWN),
        ("su1120_diverted", TripStatus.UNKNOWN),
    ],
)
async def test_status_from_recorded_fixtures(fixture: str, status: TripStatus) -> None:
    provider, _ = provider_with(load(fixture))

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == status


async def test_arrived_fixture_uses_runway_time_as_estimate() -> None:
    provider, _ = provider_with(load("su1120_arrived"))

    [flight] = await provider.search("SU1120", DAY)

    assert flight.estimated_arrival == datetime(2026, 11, 20, 8, 47, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Unknown", TripStatus.SCHEDULED),
        ("Expected", TripStatus.SCHEDULED),
        ("CheckIn", TripStatus.SCHEDULED),
        ("Boarding", TripStatus.SCHEDULED),
        ("GateClosed", TripStatus.SCHEDULED),
        ("Delayed", TripStatus.DELAYED),
        ("Departed", TripStatus.DEPARTED),
        ("EnRoute", TripStatus.DEPARTED),
        ("Approaching", TripStatus.DEPARTED),
        ("Arrived", TripStatus.LANDED),
        ("Canceled", TripStatus.CANCELLED),
        ("CanceledUncertain", TripStatus.UNKNOWN),
        ("Diverted", TripStatus.UNKNOWN),
        ("SomethingNew", TripStatus.UNKNOWN),
        ("", TripStatus.UNKNOWN),
        (None, TripStatus.UNKNOWN),
        (7, TripStatus.UNKNOWN),
    ],
)
async def test_status_table(raw: object, expected: TripStatus) -> None:
    provider, _ = provider_with([entry(status=raw)])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == expected


async def test_missing_status_key_is_unknown() -> None:
    data = entry()
    del data["status"]
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == TripStatus.UNKNOWN


@pytest.mark.parametrize(
    ("delay_minutes", "status"),
    [
        (0, TripStatus.SCHEDULED),
        (14, TripStatus.SCHEDULED),
        (15, TripStatus.DELAYED),
        (90, TripStatus.DELAYED),
        (-30, TripStatus.SCHEDULED),
    ],
)
@pytest.mark.parametrize("raw", ["Expected", "CheckIn", "Boarding", "GateClosed", "Unknown"])
async def test_delay_threshold_is_fifteen_minutes(
    raw: str, delay_minutes: int, status: TripStatus
) -> None:
    data = entry(status=raw)
    revised = ARRIVAL + timedelta(minutes=delay_minutes)
    data["arrival"]["revisedTime"] = {"utc": revised.strftime("%Y-%m-%d %H:%MZ")}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == status
    assert flight.estimated_arrival == revised


@pytest.mark.parametrize("raw", ["Departed", "EnRoute", "Approaching", "Arrived"])
async def test_late_estimate_does_not_change_non_scheduled_statuses(raw: str) -> None:
    data = entry(status=raw)
    data["arrival"]["revisedTime"] = {"utc": "2026-11-20 10:50Z"}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status != TripStatus.DELAYED


async def test_estimate_prefers_revised_then_runway_then_predicted() -> None:
    data = entry()
    data["arrival"]["predictedTime"] = {"utc": "2026-11-20 09:30Z"}
    provider, _ = provider_with([data])
    assert (await provider.search("SU1120", DAY))[0].estimated_arrival == datetime(
        2026, 11, 20, 9, 30, tzinfo=UTC
    )

    data["arrival"]["runwayTime"] = {"utc": "2026-11-20 09:10Z"}
    provider, _ = provider_with([data])
    assert (await provider.search("SU1120", DAY))[0].estimated_arrival == datetime(
        2026, 11, 20, 9, 10, tzinfo=UTC
    )

    data["arrival"]["revisedTime"] = {"utc": "2026-11-20 09:00Z"}
    provider, _ = provider_with([data])
    assert (await provider.search("SU1120", DAY))[0].estimated_arrival == datetime(
        2026, 11, 20, 9, 0, tzinfo=UTC
    )


async def test_unparsable_revised_time_falls_through_to_next() -> None:
    data = entry()
    data["arrival"]["revisedTime"] = {"utc": "завтра"}
    data["arrival"]["runwayTime"] = {"utc": "2026-11-20 09:10Z"}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.estimated_arrival == datetime(2026, 11, 20, 9, 10, tzinfo=UTC)


async def test_departure_estimates_are_not_used() -> None:
    data = entry()
    data["departure"]["revisedTime"] = {"utc": "2026-11-20 09:00Z"}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.estimated_arrival is None


async def test_time_formats_fixture() -> None:
    provider, _ = provider_with(load("time_formats"))

    [flight] = await provider.search("SU1120", DAY)

    assert flight.scheduled_departure == DEPARTURE  # `2026-11-20T07:20:00Z`
    assert flight.scheduled_arrival == ARRIVAL  # `2026-11-20 08:50:00Z`
    # `2026-11-20T12:10:00+03:00` -> 09:10Z, позже расписания на 20 минут
    assert flight.estimated_arrival == datetime(2026, 11, 20, 9, 10, tzinfo=UTC)
    assert flight.status == TripStatus.DELAYED
    assert flight.provider_ref == "adb:SU1120:2026-11-20:SVO"


@pytest.mark.parametrize(
    "raw",
    [
        "2026-11-20 07:20Z",
        "2026-11-20 07:20:00Z",
        "2026-11-20T07:20Z",
        "2026-11-20T07:20:00Z",
        "2026-11-20T07:20:00+00:00",
        "2026-11-20 10:20+03:00",
        "2026-11-20T10:20:00+03:00",
        "2026-11-20T07:20:00.000Z",
    ],
)
async def test_scheduled_time_formats(raw: str) -> None:
    data = entry()
    data["departure"]["scheduledTime"] = {"utc": raw}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.scheduled_departure == DEPARTURE
    assert flight.scheduled_departure.utcoffset() == timedelta(0)


@pytest.mark.parametrize("raw", ["2026-11-20 07:20", "2026-11-20", "garbage", "", None, 12, {}])
async def test_bad_scheduled_time_skips_record_with_warning(
    raw: object, caplog: pytest.LogCaptureFixture
) -> None:
    data = entry()
    data["departure"]["scheduledTime"] = {"utc": raw}
    provider, _ = provider_with([data])

    with caplog.at_level(logging.WARNING):
        assert await provider.search("SU1120", DAY) == []

    assert "SU1120" not in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- отбор записей -----------------------------------------------------------------------------


async def test_codeshare_operator_wins_over_marketing_record() -> None:
    provider, _ = provider_with(load("codeshare"))

    flights = await provider.search("SU1120", DAY)

    assert len(flights) == 1
    assert flights[0].status == TripStatus.DELAYED  # статус оператора, не маркетингового рейса
    assert flights[0].number == "SU1120"


async def test_codeshare_operator_wins_even_when_marketing_number_requested() -> None:
    provider, _ = provider_with(load("codeshare"))

    flights = await provider.search("AF2222", DAY)

    assert len(flights) == 1
    assert flights[0].status == TripStatus.DELAYED
    assert flights[0].number == "AF2222"  # номер, который искали: ключ поездки не прыгает
    assert flights[0].provider_ref == "adb:AF2222:2026-11-20:SVO"


async def test_number_match_decides_without_operator_record() -> None:
    other = entry(number="AF 2222", codeshareStatus="IsCodeshared", status="Departed")
    mine = entry(number="SU 1120", codeshareStatus="IsCodeshared", status="Boarding")
    provider, _ = provider_with([other, mine])

    flights = await provider.search("SU1120", DAY)

    assert [f.status for f in flights] == [TripStatus.SCHEDULED]  # Boarding


async def test_first_record_wins_when_nothing_decides() -> None:
    first = entry(number="AF 1", codeshareStatus="IsCodeshared", status="Departed")
    second = entry(number="AF 2", codeshareStatus="IsCodeshared", status="Arrived")
    provider, _ = provider_with([first, second])

    flights = await provider.search("SU1120", DAY)

    assert [f.status for f in flights] == [TripStatus.DEPARTED]


async def test_multisegment_flight_gives_one_record_per_segment() -> None:
    provider, _ = provider_with(load("multisegment"))

    flights = await provider.search("SU1120", DAY)

    assert [(f.departure_iata, f.arrival_iata) for f in flights] == [("SVO", "LED"), ("LED", "KGD")]
    assert [f.provider_ref for f in flights] == [
        "adb:SU1120:2026-11-20:SVO",
        "adb:SU1120:2026-11-20:LED",
    ]
    assert flights[1].scheduled_departure == datetime(2026, 11, 20, 10, 30, tzinfo=UTC)


async def test_cargo_records_are_dropped() -> None:
    provider, _ = provider_with(load("cargo"))
    flights = await provider.search("SU1120", DAY)
    assert [f.status for f in flights] == [TripStatus.DELAYED]

    provider, _ = provider_with(load("cargo_only"))
    assert await provider.search("SU1120", DAY) == []


async def test_record_without_iata_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = provider_with(load("no_iata"))

    with caplog.at_level(logging.WARNING):
        flights = await provider.search("SU1120", DAY)

    # битая запись пропущена, исправная того же сегмента остаётся
    assert [f.status for f in flights] == [TripStatus.SCHEDULED]
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "SU1120" not in caplog.text
    assert "SU 1120" not in caplog.text


@pytest.mark.parametrize("iata", ["", "  ", "SV", "SVOO", "S1O", None, 5])
async def test_invalid_iata_is_skipped(iata: object) -> None:
    data = entry()
    data["departure"]["airport"]["iata"] = iata
    provider, _ = provider_with([data])

    assert await provider.search("SU1120", DAY) == []


async def test_iata_is_uppercased() -> None:
    data = entry()
    data["departure"]["airport"]["iata"] = " svo "
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.departure_iata == "SVO"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("arrival"),
        lambda e: e.pop("departure"),
        lambda e: e.update(departure="x"),
        lambda e: e["arrival"].pop("scheduledTime"),
        lambda e: e["arrival"].update(scheduledTime=None),
        lambda e: e["departure"]["airport"].clear(),
    ],
)
async def test_malformed_record_is_skipped_not_fatal(
    mutate: Callable[[dict[str, Any]], Any],
) -> None:
    data = entry()
    mutate(data)
    provider, _ = provider_with([data, "строка", None, entry(status="Boarding")])

    flights = await provider.search("SU1120", DAY)

    assert [f.status for f in flights] == [TripStatus.SCHEDULED]


# --- коды ответа -------------------------------------------------------------------------------


async def test_204_is_empty_list() -> None:
    provider, _ = provider_with(None, 204)

    assert await provider.search("SU1120", DAY) == []


async def test_200_with_empty_list_is_empty_list() -> None:
    provider, _ = provider_with([])

    assert await provider.search("SU1120", DAY) == []


async def test_400_is_empty_list_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = provider_with(None, 400)

    with caplog.at_level(logging.WARNING):
        assert await provider.search("SU1120", DAY) == []

    assert "400" in caplog.text
    assert "SU1120" not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404, 429, 451, 500, 502, 503, 504, 302, 418])
async def test_error_statuses_are_unavailable(status: int) -> None:
    provider, _ = provider_with(None, status)

    with pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)
    with pytest.raises(ProviderUnavailableError):
        await provider.fetch("adb:SU1120:2026-11-20:SVO")


@pytest.mark.parametrize("body", [None, {"flights": []}, "text", 5, {"departure": {}}])
async def test_200_with_non_list_is_unavailable(body: object) -> None:
    provider, _ = provider_with(body)

    with pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        aiohttp.ClientError(),
        aiohttp.ClientConnectionError(),
        aiohttp.ServerDisconnectedError(),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_network_errors_are_unavailable(error: BaseException) -> None:
    provider, _ = provider_with(error=error)

    with pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)
    with pytest.raises(ProviderUnavailableError):
        await provider.fetch("adb:SU1120:2026-11-20:SVO")


async def test_unexpected_exception_from_http_is_not_swallowed() -> None:
    provider, _ = provider_with(error=KeyError("bug"))

    with pytest.raises(KeyError):
        await provider.search("SU1120", DAY)


# --- provider_ref и fetch ----------------------------------------------------------------------


async def test_provider_ref_uses_local_date_from_scheduled_local() -> None:
    # вылет 21:30 UTC 19 ноября = 00:30 20 ноября по Москве: дата — местная, из `local`
    data = entry()
    data["departure"]["scheduledTime"] = {
        "utc": "2026-11-19 21:30Z",
        "local": "2026-11-20 00:30+03:00",
    }
    data["arrival"]["scheduledTime"] = {"utc": "2026-11-19 23:00Z"}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.provider_ref == "adb:SU1120:2026-11-20:SVO"
    assert flight.scheduled_departure == datetime(2026, 11, 19, 21, 30, tzinfo=UTC)


async def test_provider_ref_date_falls_back_to_airport_time_zone() -> None:
    data = entry()
    data["departure"]["scheduledTime"] = {"utc": "2026-11-19 21:30Z"}
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.provider_ref == "adb:SU1120:2026-11-20:SVO"  # Europe/Moscow


async def test_provider_ref_date_falls_back_to_utc_date() -> None:
    data = entry()
    data["departure"]["scheduledTime"] = {"utc": "2026-11-19 21:30Z"}
    del data["departure"]["airport"]["timeZone"]
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.provider_ref == "adb:SU1120:2026-11-19:SVO"


async def test_provider_ref_ignores_unknown_time_zone_name() -> None:
    data = entry()
    data["departure"]["scheduledTime"] = {"utc": "2026-11-19 21:30Z"}
    data["departure"]["airport"]["timeZone"] = "Mars/Olympus"
    provider, _ = provider_with([data])

    [flight] = await provider.search("SU1120", DAY)

    assert flight.provider_ref == "adb:SU1120:2026-11-19:SVO"


async def test_fetch_repeats_search_and_returns_same_airport_record() -> None:
    provider, http = provider_with(load("multisegment"))

    flight = await provider.fetch("adb:SU1120:2026-11-20:LED")

    assert flight is not None
    assert (flight.departure_iata, flight.arrival_iata) == ("LED", "KGD")
    assert flight.provider_ref == "adb:SU1120:2026-11-20:LED"
    assert http.calls[0][0].endswith("/flights/Number/SU1120/2026-11-20")
    assert http.calls[0][2]["dateLocalRole"] == "Departure"


async def test_fetch_roundtrips_with_search() -> None:
    provider, _ = provider_with(load("su1120_delayed"))
    [found] = await provider.search("SU1120", DAY)

    assert await provider.fetch(found.provider_ref) == found


async def test_fetch_other_departure_airport_is_none() -> None:
    provider, _ = provider_with(load("su1120_scheduled"))

    assert await provider.fetch("adb:SU1120:2026-11-20:DME") is None


async def test_fetch_not_found_is_none() -> None:
    provider, _ = provider_with(None, 204)

    assert await provider.fetch("adb:SU1120:2026-11-20:SVO") is None


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "SU1120",
        "fake:SU1120:2026-11-20",
        "adb:SU1120:2026-11-20",
        "adb:SU1120:2026-11-20:SVO:extra",
        "adb::2026-11-20:SVO",
        "adb:SU1120:20-11-2026:SVO",
        "adb:SU1120:2026-13-40:SVO",
        "adb:SU1120:2026-11-20:",
        "adb:SU1120:2026-11-20:SV",
        "adb:SU1120:2026-11-20:1VO",
        "ADB:SU1120:2026-11-20:SVO",
    ],
)
async def test_fetch_broken_ref_is_none_without_request(ref: str) -> None:
    provider, http = provider_with(load("su1120_scheduled"))

    assert await provider.fetch(ref) is None
    assert http.calls == []


# --- логи --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [None, 401, 403, 429, 451, 500])
@pytest.mark.parametrize("gateway", ["direct", "apimarket", "rapidapi"])
async def test_logs_have_no_key_or_flight_number(
    gateway: Gateway, status: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    leaky = aiohttp.ClientConnectionError(f"https://x/flights/Number/SU1120 {KEY}")
    provider, _ = provider_with(
        None, status or 200, gateway=gateway, error=leaky if status is None else None
    )

    with caplog.at_level(logging.DEBUG):
        for call in (
            lambda: provider.search("SU1120", DAY),
            lambda: provider.fetch("adb:SU1120:2026-11-20:SVO"),
        ):
            with pytest.raises(ProviderUnavailableError) as info:
                await call()
            assert KEY not in str(info.value)
            assert "SU1120" not in str(info.value)
            assert info.value.__cause__ is None  # в цепочке нет исключения с URL и ключом

    assert KEY not in caplog.text
    assert "SU1120" not in caplog.text
    assert "x-api" not in caplog.text.lower()
    assert "rapidapi-key" not in caplog.text.lower()
    for record in caplog.records:
        assert KEY not in record.getMessage()
        assert record.exc_info is None or KEY not in repr(record.exc_info)


async def test_error_log_has_status_gateway_and_exception_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = provider_with(None, 429, gateway="rapidapi")
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)
    assert "429" in caplog.text
    assert "rapidapi" in caplog.text

    caplog.clear()
    provider, _ = provider_with(error=aiohttp.ClientConnectionError("boom"))
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)
    assert "ClientConnectionError" in caplog.text
    assert "boom" not in caplog.text


# --- реальный HTTP-путь на локальном сервере ---------------------------------------------------


@pytest.fixture
async def server() -> AsyncIterator[tuple[TestServer, list[web.Request]]]:
    seen: list[web.Request] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request)
        number = request.match_info["number"]
        if request.headers.get("X-Api-Key") != KEY:
            return web.Response(status=401)
        if number == "SU0000":
            return web.Response(status=204)
        if number == "SU0400":
            return web.Response(status=400)
        if number == "SU0500":
            return web.Response(status=500)
        if number == "SU0001":
            return web.Response(text="не json", content_type="application/json")
        if number == "SU0002":
            return web.json_response({"error": "x"})
        return web.Response(
            text=json.dumps(load("su1120_delayed")),
            content_type="text/plain",  # без json-типа
        )

    app = web.Application()
    app.router.add_get("/flights/Number/{number}/{day}", handler)
    test_server = TestServer(app)
    await test_server.start_server()
    try:
        yield test_server, seen
    finally:
        await test_server.close()


def local_provider(server: TestServer, key: str = KEY) -> AeroDataBoxProvider:
    return AeroDataBoxProvider(key, "direct", base_url=str(server.make_url("")).rstrip("/"))


async def test_real_http_path_sends_headers_and_parses(
    server: tuple[TestServer, list[web.Request]],
) -> None:
    test_server, seen = server
    provider = local_provider(test_server)

    [flight] = await provider.search("SU1120", DAY)

    assert flight.status == TripStatus.DELAYED
    assert flight.estimated_arrival == ARRIVAL + timedelta(minutes=40)
    request = seen[0]
    assert request.headers["X-Api-Key"] == KEY
    assert request.path == "/flights/Number/SU1120/2026-11-20"
    assert dict(request.query) == {
        "dateLocalRole": "Departure",
        "withAircraftImage": "false",
        "withLocation": "false",
    }
    assert await provider.fetch(flight.provider_ref) == flight


async def test_real_http_path_204_400_and_errors(
    server: tuple[TestServer, list[web.Request]],
) -> None:
    provider = local_provider(server[0])

    assert await provider.search("SU0000", DAY) == []
    assert await provider.search("SU0400", DAY) == []
    for number in ("SU0500", "SU0001", "SU0002"):
        with pytest.raises(ProviderUnavailableError):
            await provider.search(number, DAY)
    with pytest.raises(ProviderUnavailableError):
        await local_provider(server[0], key="wrong").search("SU1120", DAY)


async def test_real_http_path_connection_refused_is_unavailable() -> None:
    server = TestServer(web.Application())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    await server.close()  # порт свободен: соединение отклонят
    provider = AeroDataBoxProvider(KEY, "direct", base_url=base)

    with pytest.raises(ProviderUnavailableError):
        await provider.search("SU1120", DAY)


# --- сквозной сценарий: search_flights -> poll_due_trips ---------------------------------------


async def test_search_flights_and_poll_with_aerodatabox(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session:
        admin = await make_user(session)
        passenger = await make_user(session)
        event = await make_event(
            session, admin_id=admin.id, starts_on=date(2026, 11, 20), ends_on=date(2026, 11, 22)
        )
        await session.commit()
        admin_id, passenger_id, event_id = admin.id, passenger.id, event.id
    try:
        http = FakeHttp(200, load("su1120_scheduled"))
        provider = AeroDataBoxProvider(KEY, "apimarket", http=http)

        async with sessions() as session:
            user = await session.get(User, admin_id)
            assert user is not None
            [trip] = await search_flights(session, event_id, user, "su 1120", DAY, provider)
            trip_id = trip.id
            assert trip.source == TripSource.PROVIDER
            assert trip.provider_ref == "adb:SU1120:2026-11-20:SVO"
            assert (trip.number, trip.status) == ("SU1120", TripStatus.SCHEDULED)
            assert trip.estimated_arrival is None
            await make_application(
                session,
                event_id=event_id,
                trip_id=trip.id,
                user_id=passenger_id,
                status=ApplicationStatus.SEARCHING,
            )
            await session.commit()

        http.body = load("su1120_delayed")
        result = await poll_due_trips(
            sessions, provider, now=datetime.now(UTC) + timedelta(minutes=1)
        )

        assert (result.polled, result.changed) == (1, 1)
        assert http.calls[-1][0].endswith("/flights/Number/SU1120/2026-11-20")
        async with sessions() as session:
            saved = await session.scalar(select(Trip).where(Trip.id == trip_id))
            assert saved is not None
            assert saved.estimated_arrival == ARRIVAL + timedelta(minutes=40)
            assert saved.status == TripStatus.DELAYED
            assert saved.source == TripSource.PROVIDER
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == event_id))
            await session.execute(delete(User).where(User.id.in_([admin_id, passenger_id])))
            await session.commit()
