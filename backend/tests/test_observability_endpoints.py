import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import app.main as main_module
from app.db import dispose_engine, get_session
from tests.logs import JsonLogs


def readyz_count(status: str) -> float:
    labels = {"method": "GET", "route": "/readyz", "status": status}
    return REGISTRY.get_sample_value("ft_http_requests_total", labels) or 0.0


class FailingSession:
    """Сессия, у которой `execute` падает заданным исключением."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        raise self._error


class HangingSession:
    async def execute(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(30)


def use_session(app: FastAPI, session: Any) -> None:
    async def override() -> AsyncIterator[Any]:
        yield session

    app.dependency_overrides[get_session] = override


# ---------------------------------------------------------------- /readyz


async def test_readyz_ok(app: FastAPI, client: AsyncClient, db_session: AsyncSession) -> None:
    use_session(app, db_session)
    before = readyz_count("200")

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert readyz_count("200") == before + 1


async def test_readyz_with_engine_from_app_db(client: AsyncClient, migrated_database: str) -> None:
    try:
        response = await client.get("/readyz")
    finally:
        await dispose_engine()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_unavailable_when_db_fails(
    app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    use_session(app, FailingSession(OperationalError("SELECT 1", {}, OSError("db-host-secret"))))
    before = readyz_count("503")

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert readyz_count("503") == before + 1
    assert "db-host-secret" not in response.text
    assert json_logs.by_logger("app.access") == []
    [warning] = json_logs.by_logger("app.main")
    assert warning["level"] == "WARNING"
    assert warning["exc_type"] == "OperationalError"


async def test_readyz_unavailable_when_connection_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    use_session(app, FailingSession(ConnectionRefusedError()))

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_readyz_unavailable_on_timeout(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "READYZ_TIMEOUT_SECONDS", 0.01)
    use_session(app, HangingSession())

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_readyz_does_not_hide_programming_errors(app: FastAPI, client: AsyncClient) -> None:
    use_session(app, FailingSession(RuntimeError("bug")))

    response = await client.get("/readyz")

    assert response.status_code == 500


async def test_readyz_is_not_under_api_prefix(client: AsyncClient) -> None:
    response = await client.get("/api/v1/readyz")

    assert response.status_code == 404


# ---------------------------------------------------------------- /metrics


async def test_metrics_ok_without_redirect(client: AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_metrics_expose_http_series(client: AsyncClient) -> None:
    await client.get("/healthz")

    response = await client.get("/metrics")

    assert "# TYPE ft_http_requests_total counter" in response.text
    assert 'ft_http_requests_total{method="GET",route="/healthz",status="200"}' in response.text
    assert "# TYPE ft_http_request_duration_seconds histogram" in response.text
    assert 'ft_http_request_duration_seconds_count{method="GET",route="/healthz"}' in response.text


async def test_metrics_only_get(client: AsyncClient) -> None:
    response = await client.post("/metrics")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


async def test_metrics_is_not_under_api_prefix(client: AsyncClient) -> None:
    response = await client.get("/api/v1/metrics")

    assert response.status_code == 404
