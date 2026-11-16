"""Тестовый поставщик рейсов в памяти: для тестов и локальной разработки."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.flights.provider import ProviderFlight, ProviderUnavailableError
from app.models.enums import TripStatus
from app.schemas.trip import normalize_number

GENERATED_DEPARTURE_IATA = "SVO"
GENERATED_ARRIVAL_IATA = "LED"
_GENERATED_TZ = ZoneInfo("Europe/Moscow")
_GENERATED_DEPARTURE_TIME = time(10, 0)
_GENERATED_DURATION = timedelta(hours=2)


class FakeFlightProvider:
    """Рейсы хранятся в памяти.

    Поиск. Если рейсы не передавали (ни в конструктор, ни через `add`), `search` для любого номера
    и даты генерирует один рейс: SVO -> LED, вылет в 10:00 по Москве в запрошенную дату, полёт
    2 часа, статус `scheduled`, `provider_ref = "fake:<НОМЕР>:<ГГГГ-ММ-ДД>"`. Один и тот же
    запрос всегда даёт тот же рейс. Если рейсы добавляли, `search` отдаёт только подходящие
    по номеру и дате вылета (дата берётся в часовом поясе самого `scheduled_departure`).

    `fetch` отдаёт рейс по `provider_ref`: добавленный через `add`, а в режиме генерации — рейс,
    восстановленный из `provider_ref` вида `fake:<НОМЕР>:<ГГГГ-ММ-ДД>`. Память не растёт, а другой
    процесс (воркер) видит рейсы, найденные через API.

    `unavailable = True` — оба метода бросают `ProviderUnavailableError`;
    `delay` — секунды ожидания перед ответом (для проверки таймаута).
    """

    def __init__(
        self,
        flights: Iterable[ProviderFlight] = (),
        *,
        unavailable: bool = False,
        delay: float = 0.0,
    ) -> None:
        self._flights: dict[str, ProviderFlight] = {}
        self._added = False
        self.unavailable = unavailable
        self.delay = delay
        for flight in flights:
            self.add(flight)

    def add(self, flight: ProviderFlight) -> None:
        """Добавляет или заменяет рейс (по `provider_ref`) и выключает генерацию в `search`."""
        self._flights[flight.provider_ref] = flight
        self._added = True

    async def _respond(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.unavailable:
            raise ProviderUnavailableError("Тестовый поставщик недоступен")

    async def search(self, number: str, departure_date: date) -> list[ProviderFlight]:
        await self._respond()
        normalized = normalize_number(number)
        if not self._added:
            return [self._generate(normalized, departure_date)]
        return [
            flight
            for flight in self._flights.values()
            if flight.number == normalized and flight.scheduled_departure.date() == departure_date
        ]

    async def fetch(self, provider_ref: str) -> ProviderFlight | None:
        await self._respond()
        if not self._added:
            # воркер — отдельный процесс и не видел поиска в API: рейс восстанавливается из ref
            return self._restore(provider_ref)
        return self._flights.get(provider_ref)

    @classmethod
    def _restore(cls, provider_ref: str) -> ProviderFlight | None:
        prefix, _, rest = provider_ref.partition(":")
        number, _, raw_date = rest.partition(":")
        if prefix != "fake" or not number:
            return None
        try:
            departure_date = date.fromisoformat(raw_date)
        except ValueError:
            return None
        flight = cls._generate(number, departure_date)
        return flight if flight.provider_ref == provider_ref else None

    @staticmethod
    def _generate(number: str, departure_date: date) -> ProviderFlight:
        departure = datetime.combine(
            departure_date, _GENERATED_DEPARTURE_TIME, tzinfo=_GENERATED_TZ
        )
        return ProviderFlight(
            provider_ref=f"fake:{number}:{departure_date.isoformat()}",
            number=number,
            departure_iata=GENERATED_DEPARTURE_IATA,
            arrival_iata=GENERATED_ARRIVAL_IATA,
            scheduled_departure=departure.astimezone(UTC),
            scheduled_arrival=(departure + _GENERATED_DURATION).astimezone(UTC),
            estimated_arrival=None,
            status=TripStatus.SCHEDULED,
        )
