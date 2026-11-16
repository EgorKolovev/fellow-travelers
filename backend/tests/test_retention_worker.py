"""Цикл удаления данных и его запуск воркером только под флагом RETENTION_ENABLED."""

import asyncio
import logging
from typing import Any

import pytest

import app.worker.__main__ as worker_main
import app.worker.retention as retention_worker
from app.config import get_settings

TIMEOUT = 5.0
SESSION_FACTORY = object()


class Passes:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.factories: list[object] = []
        self.error = error
        self.on_call = asyncio.Event()

    async def __call__(self, session_factory: Any) -> int:
        self.calls += 1
        self.factories.append(session_factory)
        self.on_call.set()
        if self.error is not None:
            raise self.error
        return 0


@pytest.fixture
def passes(monkeypatch: pytest.MonkeyPatch) -> Passes:
    script = Passes()
    monkeypatch.setattr(retention_worker, "purge_expired", script)
    monkeypatch.setattr(retention_worker, "get_session_factory", lambda: SESSION_FACTORY)
    return script


async def test_loop_runs_a_pass_and_stops_on_stop(passes: Passes) -> None:
    stop = asyncio.Event()
    task = asyncio.ensure_future(retention_worker.run_retention(stop, interval=60))

    await asyncio.wait_for(passes.on_call.wait(), TIMEOUT)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert passes.calls == 1
    assert passes.factories == [SESSION_FACTORY]


async def test_loop_exits_at_once_when_stop_is_already_set(passes: Passes) -> None:
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(retention_worker.run_retention(stop, interval=60), TIMEOUT)

    assert passes.calls == 0


async def test_loop_repeats_after_interval(passes: Passes) -> None:
    stop = asyncio.Event()
    task = asyncio.ensure_future(retention_worker.run_retention(stop, interval=0.01))

    for _ in range(300):
        if passes.calls >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert passes.calls >= 3


async def test_loop_survives_pass_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    script = Passes(error=ConnectionRefusedError("db is down"))
    monkeypatch.setattr(retention_worker, "purge_expired", script)
    monkeypatch.setattr(retention_worker, "get_session_factory", lambda: SESSION_FACTORY)
    caplog.set_level(logging.ERROR)
    stop = asyncio.Event()
    task = asyncio.ensure_future(retention_worker.run_retention(stop, interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def run_main(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, caplog: pytest.LogCaptureFixture
) -> list[str]:
    events: list[str] = []

    class FakeServer:
        def shutdown(self) -> None:
            pass

    async def fake_run(stop: asyncio.Event) -> None:
        events.append("run")

    async def fake_retention(stop: asyncio.Event) -> None:
        events.append("retention")

    async def fake_dispose() -> None:
        pass

    class FakeSession:
        async def close(self) -> None:
            events.append("bot closed")

    class FakeBot:
        session = FakeSession()

    async def fake_notifications(stop: asyncio.Event, sender: object) -> None:
        events.append("notifications")

    monkeypatch.setattr(worker_main, "build_bot", lambda settings: FakeBot())
    monkeypatch.setattr(worker_main, "run_notification_sender", fake_notifications)
    monkeypatch.setenv("RETENTION_ENABLED", "true" if enabled else "false")
    get_settings.cache_clear()
    monkeypatch.setattr(worker_main, "start_http_server", lambda port: (FakeServer(), object()))
    monkeypatch.setattr(worker_main, "run", fake_run)
    monkeypatch.setattr(worker_main, "run_retention", fake_retention)
    monkeypatch.setattr(worker_main, "build_provider", lambda settings: None)
    monkeypatch.setattr(worker_main, "dispose_engine", fake_dispose)
    monkeypatch.setattr(worker_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_main, "_stop_on_signals", lambda loop, stop: None)
    caplog.set_level(logging.INFO, logger="app.worker")
    try:
        await worker_main.main()
    finally:
        get_settings.cache_clear()
    return events


async def test_main_does_not_start_retention_when_flag_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events = await run_main(monkeypatch, False, caplog)

    assert sorted(events) == ["bot closed", "notifications", "run"]
    assert "Удаление данных выключено" in caplog.text


async def test_main_starts_retention_when_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events = await run_main(monkeypatch, True, caplog)

    assert sorted(events) == ["bot closed", "notifications", "retention", "run"]
    assert "Удаление данных выключено" not in caplog.text
