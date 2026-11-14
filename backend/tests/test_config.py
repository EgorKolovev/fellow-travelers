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
