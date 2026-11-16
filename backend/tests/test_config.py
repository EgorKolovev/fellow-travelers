import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")


def make_settings(monkeypatch: pytest.MonkeyPatch, bot_username: str) -> Settings:
    monkeypatch.setenv("BOT_USERNAME", bot_username)
    return Settings(_env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@my_bot1", "my_bot1"),
        ("my_bot1", "my_bot1"),
        ("abcde", "abcde"),
        ("a" * 32, "a" * 32),
    ],
)
def test_bot_username_valid(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    assert make_settings(monkeypatch, raw).bot_username == expected


@pytest.mark.parametrize(
    "raw",
    ["bad name", "abc", "abcd", "@abcd", "a" * 33, "", "@", "@@my_bot1", "my-bot1", "бот_тест"],
)
def test_bot_username_invalid(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValidationError, match="BOT_USERNAME"):
        make_settings(monkeypatch, raw)


def test_log_level_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert make_settings(monkeypatch, "my_bot1").log_level == "INFO"


def test_log_level_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert make_settings(monkeypatch, "my_bot1").log_level == "DEBUG"


def make_web_app_url(monkeypatch: pytest.MonkeyPatch, raw: str) -> str | None:
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")
    monkeypatch.setenv("WEB_APP_URL", raw)
    return Settings(_env_file=None).web_app_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("poputchiki.example.com", "https://poputchiki.example.com"),
        ("https://poputchiki.example.com", "https://poputchiki.example.com"),
        ("https://poputchiki.example.com/", "https://poputchiki.example.com"),
        ("poputchiki.example.com/", "https://poputchiki.example.com"),
        ("", None),
        ("   ", None),
    ],
)
def test_web_app_url_normalized(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None
) -> None:
    assert make_web_app_url(monkeypatch, raw) == expected


def test_web_app_url_default_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")
    monkeypatch.delenv("WEB_APP_URL", raising=False)
    assert Settings(_env_file=None).web_app_url is None


@pytest.mark.parametrize("raw", ["http://poputchiki.example.com", "ftp://x.example.com"])
def test_web_app_url_requires_https(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValidationError, match="WEB_APP_URL"):
        make_web_app_url(monkeypatch, raw)


def test_flight_provider_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLIGHT_PROVIDER", raising=False)

    assert make_settings(monkeypatch, "my_bot1").flight_provider == "none"


def test_flight_provider_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_PROVIDER", "fake")

    assert make_settings(monkeypatch, "my_bot1").flight_provider == "fake"


def test_retention_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETENTION_ENABLED", raising=False)
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")

    assert Settings(_env_file=None).retention_enabled is False


def test_retention_enabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETENTION_ENABLED", "true")
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")

    assert Settings(_env_file=None).retention_enabled is True


def make_flight_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("BOT_USERNAME", "my_bot1")
    for name in ("FLIGHT_PROVIDER", "AERODATABOX_API_KEY", "AERODATABOX_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_aerodatabox_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_flight_settings(monkeypatch)

    assert settings.flight_provider == "none"
    assert settings.aerodatabox_api_key is None
    assert settings.aerodatabox_gateway == "apimarket"


def test_aerodatabox_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="AERODATABOX_API_KEY"):
        make_flight_settings(monkeypatch, FLIGHT_PROVIDER="aerodatabox")


@pytest.mark.parametrize("blank", ["", "   "])
def test_aerodatabox_blank_key_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    with pytest.raises(ValidationError, match="AERODATABOX_API_KEY"):
        make_flight_settings(monkeypatch, FLIGHT_PROVIDER="aerodatabox", AERODATABOX_API_KEY=blank)


@pytest.mark.parametrize("provider", ["none", "fake"])
def test_blank_key_does_not_break_other_providers(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings = make_flight_settings(
        monkeypatch, FLIGHT_PROVIDER=provider, AERODATABOX_API_KEY="", AERODATABOX_GATEWAY=""
    )

    assert settings.aerodatabox_api_key is None
    assert settings.aerodatabox_gateway == "apimarket"


def test_aerodatabox_key_is_secret_and_error_does_not_leak_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_flight_settings(
        monkeypatch,
        FLIGHT_PROVIDER="aerodatabox",
        AERODATABOX_API_KEY="top-secret-key",
        AERODATABOX_GATEWAY="rapidapi",
    )

    assert settings.aerodatabox_api_key is not None
    assert settings.aerodatabox_api_key.get_secret_value() == "top-secret-key"
    assert "top-secret-key" not in repr(settings)
    assert settings.aerodatabox_gateway == "rapidapi"
    with pytest.raises(ValidationError) as info:
        make_flight_settings(monkeypatch, FLIGHT_PROVIDER="aerodatabox", AERODATABOX_API_KEY=" ")
    assert "top-secret-key" not in str(info.value)


def test_aerodatabox_unknown_gateway_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="aerodatabox_gateway"):
        make_flight_settings(monkeypatch, AERODATABOX_GATEWAY="other")
