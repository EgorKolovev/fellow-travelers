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
