"""Интерфейс поставщика рейсов. Без зависимостей от FastAPI и БД."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.models.enums import TripStatus


class ProviderUnavailableError(Exception):
    """Поставщик не ответил вовремя, вернул 5xx или недоступна сеть."""


@dataclass(frozen=True)
class ProviderFlight:
    """Рейс в представлении поставщика; аэропорты — IATA-коды, время — с часовым поясом."""

    provider_ref: str
    number: str  # нормализованный: `normalize_number`, например "SU1234"
    departure_iata: str
    arrival_iata: str
    scheduled_departure: datetime
    scheduled_arrival: datetime
    estimated_arrival: datetime | None
    status: TripStatus

    def __post_init__(self) -> None:
        times = (self.scheduled_departure, self.scheduled_arrival, self.estimated_arrival)
        for value in times:
            if value is not None and value.utcoffset() is None:
                raise ValueError("Время рейса поставщика должно быть с часовым поясом")


class FlightProvider(Protocol):
    async def search(self, number: str, departure_date: date) -> list[ProviderFlight]:
        """Рейсы с нормализованным номером на дату вылета (в часовом поясе аэропорта вылета)."""
        ...

    async def fetch(self, provider_ref: str) -> ProviderFlight | None:
        """Актуальные данные рейса; `None`, если поставщик такого рейса не знает."""
        ...
