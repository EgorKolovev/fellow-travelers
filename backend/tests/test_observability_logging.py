import json
import logging
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI

from app.config import get_settings
from app.main import lifespan
from app.observability import context
from app.observability.logging import (
    EXTRA_FIELDS,
    LIBRARY_LOGGERS,
    JsonFormatter,
    configure_logging,
)
from tests.logs import capture_root_handler


@pytest.fixture(autouse=True)
def _empty_context() -> Iterator[None]:
    request_token = context.request_id.set(None)
    job_token = context.job_id.set(None)
    yield
    context.job_id.reset(job_token)
    context.request_id.reset(request_token)


def format_record(**attrs: Any) -> tuple[str, dict[str, Any]]:
    """Прогоняет через форматтер запись с заданными атрибутами; возвращает строку и её разбор."""
    attrs.setdefault("name", "tests.logging")
    attrs.setdefault("levelno", logging.INFO)
    attrs.setdefault("levelname", "INFO")
    attrs.setdefault("msg", "hello")
    line = JsonFormatter().format(logging.makeLogRecord(attrs))
    parsed: dict[str, Any] = json.loads(line)
    return line, parsed


# ---------------------------------------------------------------- форматтер


def test_basic_fields() -> None:
    line, data = format_record(created=1_700_000_000.123, msg="hello %s", args=("world",))

    assert "\n" not in line
    assert data == {
        "ts": "2023-11-14T22:13:20.123Z",
        "level": "INFO",
        "logger": "tests.logging",
        "message": "hello world",
    }


def test_ts_is_iso_utc_now() -> None:
    _, data = format_record(created=datetime.now(UTC).timestamp())

    parsed = datetime.fromisoformat(data["ts"])
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)
    assert data["ts"].endswith("Z")


def test_multiline_message_stays_on_one_line() -> None:
    line, data = format_record(msg="first\nsecond")

    assert "\n" not in line
    assert data["message"] == "first\nsecond"


def test_context_ids_included_when_set() -> None:
    job = uuid.uuid4()
    context.request_id.set("req-12345678")
    context.job_id.set(job)

    _, data = format_record()

    assert data["request_id"] == "req-12345678"
    assert data["job_id"] == str(job)


def test_context_ids_absent_when_unset() -> None:
    _, data = format_record()

    assert "request_id" not in data
    assert "job_id" not in data


def test_extra_whitelist() -> None:
    allowed = {
        "method": "GET",
        "route": "/api/v1/invites/{code}",
        "status": 200,
        "duration_ms": 1.5,
        "kind": "reconcile_trip",
        "outcome": "ok",
        "attempts": 2,
    }
    assert set(allowed) == set(EXTRA_FIELDS)
    forbidden = {
        "phone": "+79991234567",
        "telegram_id": 5550123456,
        "first_name": "Леонид",
        "username": "leonid",
        "body": '{"phone": "+79991234567"}',
        "authorization": "tma query_id=secret",
    }

    line, data = format_record(**allowed, **forbidden)

    for key, value in allowed.items():
        assert data[key] == value
    for key, value in forbidden.items():
        assert key not in data
        assert str(value) not in line


def test_non_json_value_in_whitelisted_field_does_not_break_the_line() -> None:
    _, data = format_record(route=uuid.UUID(int=1))

    assert data["route"] == str(uuid.UUID(int=1))


def test_exception_fields() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()

    line, data = format_record(exc_info=exc_info, levelno=logging.ERROR, levelname="ERROR")

    assert "\n" not in line
    assert data["exc_type"] == "ValueError"
    assert data["traceback"].startswith("Traceback (most recent call last):")
    assert "test_exception_fields" in data["traceback"]  # кадр стека
    assert "ValueError" in data["traceback"]  # имя класса
    assert "ValueError:" not in data["traceback"]  # после имени класса текста исключения нет


def test_no_exception_fields_without_exception() -> None:
    _, data = format_record()

    assert "exc_type" not in data
    assert "traceback" not in data


# ---------------------------------------------------------------- configure_logging


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_installs_single_json_handler() -> None:
    configure_logging("INFO")
    configure_logging("WARNING")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.WARNING


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_level_is_case_insensitive() -> None:
    configure_logging("debug")

    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="LOUD"):
        configure_logging("LOUD")


@pytest.mark.usefixtures("restore_logging")
def test_library_loggers_write_through_root() -> None:
    # как после dictConfig uvicorn: свои handlers и propagate=False
    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        library.addHandler(logging.NullHandler())
        library.propagate = False

    configure_logging("DEBUG")
    logs = capture_root_handler()

    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        assert library.handlers == []
        assert library.propagate is True
        library.warning("from %s", name)
    written = [r["logger"] for r in logs.records]
    assert written == [name for name in LIBRARY_LOGGERS if name != "uvicorn.access"]


@pytest.mark.usefixtures("restore_logging")
def test_uvicorn_access_log_stays_silent() -> None:
    """Путь в access-логе содержит invite-код. uvicorn пишет в него, если у логгера есть handler
    (`hasHandlers()`), а после `propagate=True` он есть у root: поэтому логгер отключён."""
    configure_logging("DEBUG")
    logs = capture_root_handler()
    access = logging.getLogger("uvicorn.access")

    assert access.hasHandlers()
    access.info('127.0.0.1:5000 - "GET /api/v1/invites/QZX742 HTTP/1.1" 200')
    access.critical("GET /api/v1/invites/QZX742")

    assert logs.text == ""


@pytest.mark.usefixtures("restore_logging")
async def test_lifespan_configures_logging_from_settings(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            root = logging.getLogger()
            assert root.level == logging.WARNING
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        get_settings.cache_clear()
