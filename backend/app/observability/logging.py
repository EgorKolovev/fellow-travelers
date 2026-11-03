"""JSON-логи в stdout: одна строка на запись, поля только из белого списка.

Телефоны, имена, username, telegram_id, initData и invite-коды в лог не попадают: форматтер
берёт из `extra` лишь перечисленные ниже ключи, а `request_id` и `job_id` — из контекста.
Текст сообщения отвечает вызывающий код: персональных данных в нём быть не должно.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.observability import context

# Ключи `extra`, которые попадают в строку. Остальные молча отбрасываются: так случайно
# переданный в `extra` телефон или тело запроса не уйдёт в лог.
EXTRA_FIELDS = ("method", "route", "status", "duration_ms", "kind", "outcome", "attempts")

# Логгеры библиотек пишут через root: свои handlers (stderr/stdout в другом формате) снимаем.
# Уровень «sqlalchemy» не трогаем: библиотека держит его на WARNING, и SQL с результатами
# запросов в лог не попадает, даже если root стоит на DEBUG.
LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "aiogram")


class JsonFormatter(logging.Formatter):
    """Форматирует запись в одну строку JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = context.request_id.get()
        if request_id is not None:
            payload["request_id"] = request_id
        job_id = context.job_id.get()
        if job_id is not None:
            payload["job_id"] = str(job_id)
        for key in EXTRA_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["traceback"] = self.formatException(record.exc_info)
        # default=str: значение из белого списка нестандартного типа не должно ронять запись
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Ставит на root один JSON-handler и направляет в него логгеры uvicorn, sqlalchemy и aiogram.

    Повторный вызов безопасен: handler остаётся один. Неизвестное имя уровня — `ValueError`.
    Логгер `uvicorn.access` отключается: см. комментарий в конце функции.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        for old in list(library.handlers):
            library.removeHandler(old)
        library.propagate = True
    # Access-лог uvicorn пишет путь запроса, а в нём invite-код; вместо него есть app.access с
    # шаблоном маршрута. Флага --no-access-log мало: uvicorn решает по hasHandlers(), а после
    # propagate=True логгер снова «видит» handler root и пишет в него.
    logging.getLogger("uvicorn.access").disabled = True
