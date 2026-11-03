import re
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    bot_token: SecretStr
    bot_username: str
    init_data_max_age_seconds: int = 86400
    debug: bool = False
    log_level: str = "INFO"
    # порт, на котором воркер отдаёт метрики Prometheus (только внутри сети compose)
    worker_metrics_port: int = Field(default=9100, ge=1, le=65535)

    @field_validator("bot_username")
    @classmethod
    def _check_bot_username(cls, value: str) -> str:
        """Имя бота идёт в ссылки t.me: снимаем ведущий «@» и падаем на старте при мусоре."""
        name = value.removeprefix("@")
        if not _BOT_USERNAME.fullmatch(name):
            raise ValueError("BOT_USERNAME: ожидается имя бота из 5–32 символов [A-Za-z0-9_]")
        return name


@lru_cache
def get_settings() -> Settings:
    return Settings()
