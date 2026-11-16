from app.config import Settings
from app.flights.aerodatabox import AeroDataBoxProvider
from app.flights.fake import FakeFlightProvider
from app.flights.provider import FlightProvider


def build_provider(settings: Settings) -> FlightProvider | None:
    """Поставщик по настройке `FLIGHT_PROVIDER`; `None` — не настроен."""
    if settings.flight_provider == "fake":
        return FakeFlightProvider()
    if settings.flight_provider == "aerodatabox":
        key = settings.aerodatabox_api_key
        if key is None:  # Settings уже отказал бы; страховка для mypy и ручной сборки
            raise ValueError("AERODATABOX_API_KEY обязателен при FLIGHT_PROVIDER=aerodatabox")
        return AeroDataBoxProvider(key.get_secret_value(), settings.aerodatabox_gateway)
    return None
