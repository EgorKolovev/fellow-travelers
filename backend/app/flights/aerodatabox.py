"""Адаптер AeroDataBox (M-01a): поиск рейса по номеру и дате, разбор `FlightContract[]`.

В логах только код ответа, шлюз и имя класса ошибки: ни ключа, ни заголовков, ни URL с номером
рейса. Исключения сети в цепочку `ProviderUnavailableError` не попадают: в их тексте бывает URL.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from app.flights.provider import ProviderFlight, ProviderUnavailableError
from app.models.enums import TripStatus
from app.schemas.trip import normalize_number

logger = logging.getLogger(__name__)

Gateway = Literal["direct", "apimarket", "rapidapi"]
HttpFunc = Callable[[str, dict[str, str], dict[str, str]], Awaitable[tuple[int, Any]]]

REQUEST_TIMEOUT_SECONDS = 10.0
DELAY_THRESHOLD = timedelta(minutes=15)
_BASE_URLS: dict[str, str] = {
    "direct": "https://api.aerodatabox.com",
    "apimarket": "https://prod.api.market/api/v1/aedbx/aerodatabox",
    "rapidapi": "https://aerodatabox.p.rapidapi.com",
}
_PARAMS = {"dateLocalRole": "Departure", "withAircraftImage": "false", "withLocation": "false"}
_REF_PREFIX = "adb"
_IATA = re.compile(r"[A-Z]{3}")
_LOCAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLAIN_SCHEDULED = {"Unknown", "Expected", "CheckIn", "Boarding", "GateClosed"}
_STATUSES: dict[str, TripStatus] = {
    "Delayed": TripStatus.DELAYED,
    "Departed": TripStatus.DEPARTED,
    "EnRoute": TripStatus.DEPARTED,
    "Approaching": TripStatus.DEPARTED,
    "Arrived": TripStatus.LANDED,
    "Canceled": TripStatus.CANCELLED,
}
_ESTIMATE_KEYS = ("revisedTime", "runwayTime", "predictedTime")


async def _aiohttp_get(
    url: str, headers: dict[str, str], params: dict[str, str]
) -> tuple[int, Any]:
    """Запрос на aiohttp: сессия на запрос, таймаут 10 с. Не-200 — без тела, плохой JSON — None."""
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(url, headers=headers, params=params) as response,
    ):
        if response.status != 200:
            return response.status, None
        try:
            return 200, await response.json(content_type=None)
        except ValueError:
            return 200, None


def _headers(api_key: str, gateway: Gateway) -> dict[str, str]:
    if gateway == "direct":
        return {"X-Api-Key": api_key}
    if gateway == "apimarket":
        return {"x-api-market-key": api_key}
    return {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _parse_utc(value: Any) -> datetime | None:
    """`2026-11-17 10:59Z`, `...10:59:00Z`, ISO 8601 с `T` и смещением -> UTC; без пояса — None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _local_date(leg: Any, scheduled: datetime) -> date:
    """Местная дата вылета: `scheduledTime.local`, иначе пояс аэропорта, иначе дата UTC."""
    local = _dig(leg, "scheduledTime", "local")
    if isinstance(local, str) and _LOCAL_DATE.match(local.strip()):
        try:
            return date.fromisoformat(local.strip()[:10])
        except ValueError:
            pass
    zone = _dig(leg, "airport", "timeZone")
    if isinstance(zone, str) and zone:
        try:
            return scheduled.astimezone(ZoneInfo(zone)).date()
        except (ValueError, OSError, KeyError):  # неизвестное имя пояса (ZoneInfoNotFoundError)
            pass
    return scheduled.date()


def _iata(leg: Any) -> str | None:
    raw = _dig(leg, "airport", "iata")
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    return code if _IATA.fullmatch(code) else None


def _status(raw: Any, scheduled_arrival: datetime, estimated: datetime | None) -> TripStatus:
    if not isinstance(raw, str):
        return TripStatus.UNKNOWN
    if raw in _PLAIN_SCHEDULED:
        if estimated is not None and estimated - scheduled_arrival >= DELAY_THRESHOLD:
            return TripStatus.DELAYED
        return TripStatus.SCHEDULED
    return _STATUSES.get(raw, TripStatus.UNKNOWN)


def _build_ref(number: str, day: date, iata: str) -> str:
    return f"{_REF_PREFIX}:{number}:{day.isoformat()}:{iata}"


def _parse_ref(provider_ref: str) -> tuple[str, date, str] | None:
    parts = provider_ref.split(":")
    if len(parts) != 4 or parts[0] != _REF_PREFIX:
        return None
    _, number, raw_date, iata = parts
    if not number or not _IATA.fullmatch(iata) or not _LOCAL_DATE.fullmatch(raw_date):
        return None
    try:
        return number, date.fromisoformat(raw_date), iata
    except ValueError:
        return None


class AeroDataBoxProvider:
    """Поставщик рейсов AeroDataBox через шлюз `direct`, `apimarket` или `rapidapi`.

    `http(url, headers, params) -> (status, json | None)` подменяют в тестах; по умолчанию —
    aiohttp. `base_url` заменяет адрес шлюза (тест на локальном сервере).
    """

    def __init__(
        self,
        api_key: str,
        gateway: Gateway,
        *,
        http: HttpFunc | None = None,
        base_url: str | None = None,
    ) -> None:
        if gateway not in _BASE_URLS:
            raise ValueError(f"Неизвестный gateway AeroDataBox: {gateway!r}")
        self.gateway: Gateway = gateway
        self._base_url = (base_url or _BASE_URLS[gateway]).rstrip("/")
        self._headers = _headers(api_key, gateway)
        self._http: HttpFunc = http or _aiohttp_get

    def __repr__(self) -> str:
        return f"AeroDataBoxProvider(gateway={self.gateway!r})"

    async def search(self, number: str, departure_date: date) -> list[ProviderFlight]:
        normalized = normalize_number(number)
        url = (
            f"{self._base_url}/flights/Number/{quote(normalized, safe='')}/"
            f"{quote(departure_date.isoformat(), safe='')}"
        )
        try:
            status, body = await self._http(url, dict(self._headers), dict(_PARAMS))
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning(
                "AeroDataBox: ошибка запроса (%s), шлюз %s", type(exc).__name__, self.gateway
            )
            raise ProviderUnavailableError("AeroDataBox: ошибка сети или таймаут") from None
        if status == 204:
            return []
        if status == 400:
            logger.warning("AeroDataBox: код ответа 400, шлюз %s", self.gateway)
            return []
        if status != 200:
            logger.warning("AeroDataBox: код ответа %s, шлюз %s", status, self.gateway)
            raise ProviderUnavailableError(f"AeroDataBox: код ответа {status}")
        if not isinstance(body, list):
            logger.warning("AeroDataBox: ответ не по схеме, шлюз %s", self.gateway)
            raise ProviderUnavailableError("AeroDataBox: ответ не по схеме")
        return self._select(body, normalized)

    async def fetch(self, provider_ref: str) -> ProviderFlight | None:
        parsed = _parse_ref(provider_ref)
        if parsed is None:
            return None
        number, day, iata = parsed
        for flight in await self.search(number, day):
            if flight.departure_iata == iata:
                return flight
        return None

    def _select(self, records: list[Any], number: str) -> list[ProviderFlight]:
        """Отбор: без грузовых; на сегмент (аэропорт вылета) — оператор, затем совпадение номера."""
        best: dict[str, tuple[tuple[bool, bool], ProviderFlight]] = {}
        for record in records:
            if not isinstance(record, dict) or record.get("isCargo") is True:
                continue
            flight = self._parse(record, number)
            if flight is None:
                continue
            raw_number = record.get("number")
            matches = isinstance(raw_number, str) and normalize_number(raw_number) == number
            rank = (record.get("codeshareStatus") == "IsOperator", matches)
            current = best.get(flight.departure_iata)
            if current is None or rank > current[0]:
                best[flight.departure_iata] = (rank, flight)
        return [flight for _, flight in best.values()]

    def _parse(self, record: dict[str, Any], number: str) -> ProviderFlight | None:
        departure = record.get("departure")
        arrival = record.get("arrival")
        departure_iata = _iata(departure)
        arrival_iata = _iata(arrival)
        scheduled_departure = _parse_utc(_dig(departure, "scheduledTime", "utc"))
        scheduled_arrival = _parse_utc(_dig(arrival, "scheduledTime", "utc"))
        if (
            departure_iata is None
            or arrival_iata is None
            or scheduled_departure is None
            or scheduled_arrival is None
        ):
            logger.warning(
                "AeroDataBox: запись пропущена: нет IATA аэропорта или расписания, шлюз %s",
                self.gateway,
            )
            return None
        estimated = next(
            (
                moment
                for key in _ESTIMATE_KEYS
                if (moment := _parse_utc(_dig(arrival, key, "utc"))) is not None
            ),
            None,
        )
        return ProviderFlight(
            provider_ref=_build_ref(
                number, _local_date(departure, scheduled_departure), departure_iata
            ),
            number=number,
            departure_iata=departure_iata,
            arrival_iata=arrival_iata,
            scheduled_departure=scheduled_departure,
            scheduled_arrival=scheduled_arrival,
            estimated_arrival=estimated,
            status=_status(record.get("status"), scheduled_arrival, estimated),
        )


__all__ = ["AeroDataBoxProvider", "Gateway"]
