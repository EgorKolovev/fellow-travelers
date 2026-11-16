from app.config import Settings
from app.flights.fake import FakeFlightProvider
from app.flights.provider import FlightProvider


def build_provider(settings: Settings) -> FlightProvider | None:
    """Поставщик по настройке `FLIGHT_PROVIDER`; `None` — не настроен."""
    if settings.flight_provider == "fake":
        return FakeFlightProvider()
    return None
