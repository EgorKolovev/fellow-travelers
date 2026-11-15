"""Фикстура JSON-логов: приложение пишет через настоящий `configure_logging`, тест читает строки."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.observability.logging import LIBRARY_LOGGERS, configure_logging

# httpx и httpcore — клиент теста, а не приложение: их INFO-строки содержат URL запроса
_TEST_CLIENT_LOGGERS = ("httpx", "httpcore")


class JsonLogs:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    @property
    def records(self) -> list[dict[str, Any]]:
        """Каждая строка вывода обязана быть отдельным JSON-объектом."""
        parsed: list[dict[str, Any]] = [json.loads(line) for line in self.text.splitlines()]
        return parsed

    def by_logger(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["logger"] == name]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """После теста возвращает root и логгеры библиотек в прежнее состояние."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_libraries = [
        (lib, list(lib.handlers), lib.propagate, lib.disabled)
        for lib in (logging.getLogger(name) for name in LIBRARY_LOGGERS)
    ]
    try:
        yield
    finally:
        for current in list(root.handlers):
            root.removeHandler(current)
        for old in saved_handlers:
            root.addHandler(old)
        root.setLevel(saved_level)
        for lib, handlers, propagate, disabled in saved_libraries:
            for current in list(lib.handlers):
                lib.removeHandler(current)
            for old in handlers:
                lib.addHandler(old)
            lib.propagate = propagate
            lib.disabled = disabled


def capture_root_handler() -> JsonLogs:
    """Направляет вывод единственного handler'а root (после `configure_logging`) в память."""
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    stream = io.StringIO()
    handler.setStream(stream)
    handler.addFilter(lambda record: not record.name.startswith(_TEST_CLIENT_LOGGERS))
    return JsonLogs(stream)


@pytest.fixture
def json_logs(restore_logging: None) -> JsonLogs:
    """`configure_logging("DEBUG")` с выводом в память: приложение пишет через настоящий handler.

    Тестам с БД просить эту фикстуру раньше `db_session`: соединение запоминает, нужно ли ему
    логировать SQL, в момент создания.
    """
    configure_logging("DEBUG")
    return capture_root_handler()
