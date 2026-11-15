"""В JSON-логах нет текста исключений: в нём бывают значения строк БД, то есть телефоны.

Форматтер пишет в `traceback` только кадры стека и имена классов цепочки. Здесь он проверяется
на каждом месте, где приложение пишет исключение в лог: обработчик 500, цикл воркера, `/readyz`
и учёт неудачной попытки задания. Сообщение у всех одно, SECRET, и в выводе его быть не должно.
"""

import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import app.worker as worker
from app.observability.logging import JsonFormatter
from app.services import jobs as jobs_service
from app.services.jobs import JobOutcome, process_next
from app.services.matching import ReconcileResult
from tests.factories import make_job
from tests.helpers.jobs import shared_session_factory
from tests.logs import JsonLogs
from tests.test_observability_endpoints import FailingSession, use_session

SECRET = "SECRET-+79991234567-Ivan"


def format_exception(error: BaseException) -> dict[str, Any]:
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "msg", None, (type(error), error, error.__traceback__)
    )
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


# ---------------------------------------------------------------- форматтер


def failing_operation() -> None:
    raise ValueError(SECRET)


def wrapping_operation() -> None:
    try:
        failing_operation()
    except ValueError as exc:
        raise RuntimeError(SECRET) from exc


def reacting_operation() -> None:
    try:
        failing_operation()
    except ValueError:
        raise KeyError(SECRET) from None


def raising_in_handler() -> None:
    try:
        failing_operation()
    except ValueError:
        raise LookupError(SECRET)  # noqa: B904 - цепочка по контексту нужна тесту


def captured(function: Any) -> BaseException:
    try:
        function()
    except BaseException as exc:
        return exc
    raise AssertionError("исключения не было")


def test_exception_message_is_not_in_the_line() -> None:
    data = format_exception(captured(failing_operation))

    assert SECRET not in str(data)
    assert data["exc_type"] == "ValueError"
    assert "failing_operation" in data["traceback"]
    assert data["traceback"].rstrip().endswith("ValueError")


def test_explicit_chain_keeps_both_classes_and_frames_without_text() -> None:
    data = format_exception(captured(wrapping_operation))

    traceback_text = data["traceback"]
    assert SECRET not in str(data)
    assert data["exc_type"] == "RuntimeError"
    assert "failing_operation" in traceback_text
    assert "wrapping_operation" in traceback_text
    # первопричина идёт раньше и названа связкой, как в обычном traceback
    assert traceback_text.index("ValueError") < traceback_text.index("RuntimeError")
    assert "direct cause" in traceback_text


def test_implicit_context_is_kept_without_text() -> None:
    data = format_exception(captured(raising_in_handler))

    assert SECRET not in str(data)
    assert data["exc_type"] == "LookupError"
    assert "During handling of the above exception" in data["traceback"]
    assert data["traceback"].index("ValueError") < data["traceback"].index("LookupError")


def test_suppressed_context_is_not_printed() -> None:
    data = format_exception(captured(reacting_operation))

    assert SECRET not in str(data)
    assert "ValueError" not in data["traceback"]
    assert data["traceback"].rstrip().endswith("KeyError")


def test_database_error_text_with_detail_is_dropped() -> None:
    detail = f"DETAIL:  Key (phone)=({SECRET}) already exists."
    error = OperationalError("INSERT INTO users", {}, OSError(detail))

    data = format_exception(error)

    assert SECRET not in str(data)
    assert data["exc_type"] == "OperationalError"
    assert "\n" not in JsonFormatter().format(
        logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "msg", None, (type(error), error, None)
        )
    )  # одна запись — одна строка вывода


# ---------------------------------------------------------------- места, где пишется исключение


@pytest.fixture
def exploding_app(app: FastAPI) -> FastAPI:
    @app.get("/explode")
    async def explode() -> None:
        failing_operation()

    return app


async def test_unhandled_exception_handler_does_not_log_message(
    exploding_app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    response = await client.get("/explode")

    assert response.status_code == 500
    assert SECRET not in json_logs.text
    assert SECRET not in response.text
    [unhandled] = json_logs.by_logger("app.errors")
    assert unhandled["exc_type"] == "ValueError"
    assert "failing_operation" in unhandled["traceback"]


async def test_readiness_failure_does_not_log_message(
    app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    use_session(app, FailingSession(OperationalError("SELECT 1", {}, OSError(SECRET))))

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert SECRET not in json_logs.text
    [warning] = [r for r in json_logs.records if r["message"] == "readiness check failed"]
    assert warning["exc_type"] == "OperationalError"
    assert "readyz" in warning["traceback"]


async def test_worker_loop_error_does_not_log_message(
    monkeypatch: pytest.MonkeyPatch, json_logs: JsonLogs
) -> None:
    async def fail(session_factory: Any) -> JobOutcome | None:
        raise ConnectionError(SECRET)

    async def no_snapshot(session_factory: Any) -> None:
        return None

    monkeypatch.setattr(worker, "get_session_factory", lambda: object())
    monkeypatch.setattr(worker, "observe_queue", no_snapshot)
    monkeypatch.setattr(worker, "process_next", fail)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.01))

    for _ in range(300):
        if json_logs.by_logger("app.worker"):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert SECRET not in json_logs.text
    entry = json_logs.by_logger("app.worker")[0]
    assert entry["exc_type"] == "ConnectionError"
    assert "fail" in entry["traceback"]


@pytest.mark.parametrize("attempts", [1, 8], ids=["retry", "failed"])
async def test_failed_job_attempt_does_not_log_message(
    json_logs: JsonLogs,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> ReconcileResult:
        raise LookupError(SECRET)

    monkeypatch.setattr(jobs_service, "reconcile_event", fail)
    await make_job(db_session, attempts=attempts - 1)

    outcome = await process_next(shared_session_factory(db_session))

    assert outcome in (JobOutcome.RETRY, JobOutcome.FAILED)
    assert SECRET not in json_logs.text
    [entry] = json_logs.by_logger("app.services.jobs")
    assert entry["exc_type"] == "LookupError"
    assert "fail" in entry["traceback"]
    assert entry["job_id"]
