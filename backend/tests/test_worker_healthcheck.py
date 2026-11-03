"""Healthcheck воркера: 0 при свежем опросе, не 0 при старом, нулевом и при недоступном порте."""

import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from wsgiref.simple_server import WSGIServer

import pytest
from prometheus_client import start_http_server

from app.config import get_settings
from app.observability import metrics
from app.worker import healthcheck


@pytest.fixture
def metrics_server() -> Iterator[tuple[WSGIServer, int]]:
    """Настоящий HTTP-сервер метрик на свободном порту; gauge опроса возвращается как был."""
    server, _ = start_http_server(0, addr="127.0.0.1")
    previous = healthcheck_gauge_value()
    try:
        yield server, server.server_port
    finally:
        metrics.worker_last_poll_timestamp_seconds.set(previous)
        server.shutdown()
        server.server_close()


def healthcheck_gauge_value() -> float:
    return float(metrics.worker_last_poll_timestamp_seconds._value.get())


@pytest.fixture
def closed_port() -> int:
    """Порт, на котором только что слушал сервер и больше никто не слушает."""
    server, _ = start_http_server(0, addr="127.0.0.1")
    port = server.server_port
    server.shutdown()
    server.server_close()
    return port


def test_fresh_poll_is_healthy(metrics_server: tuple[WSGIServer, int]) -> None:
    _, port = metrics_server
    metrics.worker_last_poll_timestamp_seconds.set(time.time() - 5)

    age = healthcheck.last_poll_age(port)

    assert age is not None
    assert 4 < age < 10
    assert healthcheck.is_healthy(port)


def test_stale_poll_is_unhealthy(metrics_server: tuple[WSGIServer, int]) -> None:
    _, port = metrics_server
    metrics.worker_last_poll_timestamp_seconds.set(time.time() - 120)

    assert not healthcheck.is_healthy(port)


def test_poll_just_inside_the_limit_is_healthy(metrics_server: tuple[WSGIServer, int]) -> None:
    _, port = metrics_server
    moment = time.time()
    metrics.worker_last_poll_timestamp_seconds.set(moment - 59)

    assert healthcheck.is_healthy(port, now=moment)
    assert not healthcheck.is_healthy(port, now=moment + 2)


def test_no_poll_yet_is_unhealthy(metrics_server: tuple[WSGIServer, int]) -> None:
    _, port = metrics_server
    metrics.worker_last_poll_timestamp_seconds.set(0)  # значение до первого опроса

    assert healthcheck.last_poll_age(port) is None
    assert not healthcheck.is_healthy(port)


def test_unreachable_port_is_unhealthy(closed_port: int) -> None:
    assert healthcheck.last_poll_age(closed_port) is None
    assert not healthcheck.is_healthy(closed_port)


def test_garbage_response_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ft_worker_last_poll_timestamp_seconds not-a-number\n"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())

    assert not healthcheck.is_healthy(9100)


@pytest.mark.parametrize(("age", "code"), [(5, 0), (120, 1)])
def test_main_reads_port_from_settings(
    metrics_server: tuple[WSGIServer, int],
    monkeypatch: pytest.MonkeyPatch,
    age: float,
    code: int,
) -> None:
    _, port = metrics_server
    metrics.worker_last_poll_timestamp_seconds.set(time.time() - age)
    monkeypatch.setenv("WORKER_METRICS_PORT", str(port))
    get_settings.cache_clear()
    try:
        assert healthcheck.main() == code
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.parametrize(("age", "code"), [(5, 0), (120, 1)])
def test_module_exit_code(metrics_server: tuple[WSGIServer, int], age: float, code: int) -> None:
    """Так Docker вызывает проверку: `python -m app.worker.healthcheck`, код процесса."""
    _, port = metrics_server
    metrics.worker_last_poll_timestamp_seconds.set(time.time() - age)
    env = {**os.environ, "WORKER_METRICS_PORT": str(port)}

    result = subprocess.run(
        [sys.executable, "-m", "app.worker.healthcheck"],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == code, result.stderr.decode()


def test_module_exit_code_without_server(closed_port: int) -> None:
    env = {**os.environ, "WORKER_METRICS_PORT": str(closed_port)}

    result = subprocess.run(
        [sys.executable, "-m", "app.worker.healthcheck"],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1


def test_settings_default_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_METRICS_PORT", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().worker_metrics_port == 9100
        monkeypatch.setenv("WORKER_METRICS_PORT", "70000")
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="worker_metrics_port"):
            get_settings()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
