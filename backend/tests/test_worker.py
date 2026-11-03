"""Цикл воркера `app.worker.run`: остановка по `stop`, пауза между опросами, живучесть при ошибках.

`process_next` и фабрика сессий подменены: БД не нужна, проверяется только устройство цикла.
"""

import asyncio
import logging
import os
import signal
from typing import Any

import pytest

import app.worker as worker
from app.services.jobs import JobOutcome
from app.worker.__main__ import _stop_on_signals

TIMEOUT = 5.0
SESSION_FACTORY = object()


class Script:
    """Подмена `process_next`: отдаёт заготовленные результаты, исключения пропускает как есть."""

    def __init__(
        self, *results: JobOutcome | Exception | None, then: JobOutcome | Exception | None = None
    ):
        self._results = list(results)
        self._then = then
        self.calls = 0
        self.seen_factories: list[object] = []
        self.on_call: asyncio.Event = asyncio.Event()

    async def __call__(self, session_factory: Any) -> JobOutcome | None:
        self.calls += 1
        self.seen_factories.append(session_factory)
        self.on_call.set()
        result = self._results.pop(0) if self._results else self._then
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "get_session_factory", lambda: SESSION_FACTORY)


def install(monkeypatch: pytest.MonkeyPatch, script: Script) -> None:
    monkeypatch.setattr(worker, "process_next", script)


async def test_run_exits_at_once_when_stop_is_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script()
    install(monkeypatch, script)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(worker.run(stop, poll_interval=60), TIMEOUT)

    assert script.calls == 0


async def test_stop_interrupts_the_wait_between_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(then=None)
    install(monkeypatch, script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=60))

    await asyncio.wait_for(script.on_call.wait(), TIMEOUT)
    assert not task.done()  # после пустого опроса воркер ждёт, а не крутится
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls == 1
    assert script.seen_factories == [SESSION_FACTORY]


async def test_run_drains_the_queue_before_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(JobOutcome.DONE, JobOutcome.RETRY, JobOutcome.FAILED, None)
    install(monkeypatch, script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=60))

    for _ in range(200):
        if script.calls >= 4:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    # три задания подряд без пауз, пустой опрос — и ожидание до stop
    assert script.calls == 4


async def test_run_polls_again_after_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(then=None)
    install(monkeypatch, script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3


async def test_run_finishes_the_current_job_and_does_not_take_next_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls = 0

    async def process(session_factory: Any) -> JobOutcome | None:
        nonlocal calls
        calls += 1
        stop.set()  # SIGTERM пришёл посреди задания
        await asyncio.sleep(0.01)
        return JobOutcome.DONE  # задание дошло до конца, а в очереди есть следующее

    monkeypatch.setattr(worker, "process_next", process)

    await asyncio.wait_for(worker.run(stop, poll_interval=60), TIMEOUT)

    assert calls == 1


async def test_run_survives_loop_error_logs_it_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    script = Script(ConnectionRefusedError("db is down"), then=None)
    install(monkeypatch, script)
    caplog.set_level(logging.ERROR)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    assert not task.done()  # ошибка цикла не завершила воркер
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    (record,) = [r for r in caplog.records if r.name == worker.__name__]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is ConnectionRefusedError


async def test_run_pauses_after_error_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Script(then=RuntimeError("always"))
    install(monkeypatch, script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.2))

    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert 1 <= script.calls <= 4  # с паузой 0,2 с за 0,5 с не больше нескольких попыток


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signals_only_set_the_stop_event(sig: signal.Signals) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _stop_on_signals(loop, stop)
    try:
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), TIMEOUT)
    finally:
        for registered in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(registered)

    assert stop.is_set()
