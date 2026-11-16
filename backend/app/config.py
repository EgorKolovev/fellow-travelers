import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


class Settings(BaseSettings):
    # hide_input_in_errors: ошибка конфигурации не печатает значения (токен, строку подключения)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str
    bot_token: SecretStr
    bot_username: str
    telegram_relay_url: HttpUrl | None = None
    telegram_relay_secret: SecretStr = SecretStr("")
    init_data_max_age_seconds: int = 86400
    debug: bool = False
    log_level: str = "INFO"
    # порт, на котором воркер отдаёт метрики Prometheus (только внутри сети compose)
    worker_metrics_port: int = Field(default=9100, ge=1, le=65535)
    web_app_url: str | None = None
    # поставщик рейсов: none — не настроен (поиск отвечает 503), fake — тестовый, в памяти
    flight_provider: Literal["none", "fake", "aerodatabox"] = "none"
    # ключ AeroDataBox нужен только при FLIGHT_PROVIDER=aerodatabox; пустая строка = не задан
    aerodatabox_api_key: SecretStr | None = None
    aerodatabox_gateway: Literal["direct", "apimarket", "rapidapi"] = "apimarket"
    # необратимое удаление данных групп через 7 суток после окончания (Д11); по умолчанию выключено
    retention_enabled: bool = False

    @field_validator("bot_username")
    @classmethod
    def _check_bot_username(cls, value: str) -> str:
        """Имя бота идёт в ссылки t.me: снимаем ведущий «@» и падаем на старте при мусоре."""
        name = value.removeprefix("@")
        if not _BOT_USERNAME.fullmatch(name):
            raise ValueError("BOT_USERNAME: ожидается имя бота из 5–32 символов [A-Za-z0-9_]")
        return name

    @field_validator("web_app_url")
    @classmethod
    def _check_web_app_url(cls, value: str | None) -> str | None:
        """Адрес мини-приложения для кнопки меню: пусто → None, голый домен → https://."""
        if value is None:
            return None
        url = value.strip()
        if not url:
            return None
        if "://" not in url:
            url = f"https://{url}"
        if not url.startswith("https://"):
            raise ValueError("WEB_APP_URL: ожидается адрес с https://")
        return url.rstrip("/")

    @field_validator("aerodatabox_api_key", mode="before")
    @classmethod
    def _blank_api_key_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("aerodatabox_gateway", mode="before")
    @classmethod
    def _blank_gateway_is_default(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return "apimarket"
        return value

    @model_validator(mode="after")
    def validate_flight_provider(self) -> Self:
        if self.flight_provider == "aerodatabox" and self.aerodatabox_api_key is None:
            raise ValueError("AERODATABOX_API_KEY обязателен при FLIGHT_PROVIDER=aerodatabox")
        return self

    @field_validator("telegram_relay_url", mode="before")
    @classmethod
    def empty_relay_url(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_relay(self) -> Self:
        url = self.telegram_relay_url
        if bool(url) != bool(self.telegram_relay_secret.get_secret_value()):
            raise ValueError("TELEGRAM_RELAY_URL и TELEGRAM_RELAY_SECRET задаются вместе")
        if url and (
            url.scheme != "https"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in (None, "/")
        ):
            raise ValueError("TELEGRAM_RELAY_URL должен быть HTTPS-адресом без пути и параметров")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
