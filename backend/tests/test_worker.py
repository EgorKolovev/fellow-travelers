"""Цикл воркера `app.worker.run`: остановка по `stop`, пауза между опросами, живучесть при ошибках.

`process_next` и фабрика сессий подменены: БД не нужна, проверяется только устройство цикла.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any

import pytest
from prometheus_client import REGISTRY

import app.worker as worker
import app.worker.__main__ as worker_main
from app.config import get_settings
from app.observability import metrics
from app.services.jobs import JobOutcome
from app.services.notification_sender import SendResult

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


class QueuePolls:
    """Подмена `observe_queue`: считает опросы; `error` заставляет его падать, как БД без связи."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self, session_factory: Any) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


@pytest.fixture(autouse=True)
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "get_session_factory", lambda: SESSION_FACTORY)


@pytest.fixture(autouse=True)
def queue_polls(monkeypatch: pytest.MonkeyPatch) -> QueuePolls:
    polls = QueuePolls()
    monkeypatch.setattr(worker, "observe_queue", polls)
    return polls


def last_poll() -> float:
    return REGISTRY.get_sample_value("ft_worker_last_poll_timestamp_seconds") or 0.0


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


# Windows: os.kill(pid, SIGTERM) — это TerminateProcess: убивает pytest, сигнал не доставляется.
@pytest.mark.skipif(sys.platform == "win32", reason="os.kill SIGTERM работает только на POSIX")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signals_only_set_the_stop_event(sig: signal.Signals) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    worker_main._stop_on_signals(loop, stop)
    try:
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), TIMEOUT)
    finally:
        for registered in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(registered)

    assert stop.is_set()


# ---------------------------------------------------------------- метрики цикла


async def test_each_poll_snapshots_queue_and_stamps_last_poll(
    monkeypatch: pytest.MonkeyPatch, queue_polls: QueuePolls
) -> None:
    script = Script(then=None)
    install(monkeypatch, script)
    started = time.time()
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert queue_polls.calls >= 3  # снимок очереди на каждом опросе
    assert started <= last_poll() <= time.time()


async def test_queue_snapshot_comes_before_taking_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def snapshot(session_factory: Any) -> None:
        order.append("snapshot")

    async def process(session_factory: Any) -> JobOutcome | None:
        order.append("job")
        return None

    monkeypatch.setattr(worker, "observe_queue", snapshot)
    monkeypatch.setattr(worker, "process_next", process)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=60))
    for _ in range(200):
        if len(order) >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert order == ["snapshot", "job"]


async def test_last_poll_is_not_stamped_while_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, queue_polls: QueuePolls
) -> None:
    script = Script(then=None)
    install(monkeypatch, script)
    queue_polls.error = ConnectionRefusedError("db is down")
    metrics.worker_last_poll_timestamp_seconds.set(0)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop, poll_interval=0.01))

    for _ in range(300):
        if queue_polls.calls >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert queue_polls.calls >= 3
    assert last_poll() == 0  # цикл жив, но БД не видит: healthcheck должен покраснеть
    assert script.calls == 0


def _fake_notifications(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Бот и цикл отправки подменены: сети нет, видно только порядок запуска и закрытия."""

    class FakeSession:
        async def close(self) -> None:
            events.append("bot closed")

    class FakeBot:
        session = FakeSession()

    async def fake_notifications(stop: asyncio.Event, sender: object) -> None:
        events.append("notifications")

    monkeypatch.setattr(worker_main, "build_bot", lambda settings: FakeBot())
    monkeypatch.setattr(worker_main, "run_notification_sender", fake_notifications)


async def test_main_starts_metrics_server_runs_worker_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeServer:
        def shutdown(self) -> None:
            events.append("server shutdown")

    def start_http_server(port: int) -> tuple[FakeServer, object]:
        events.append(f"server start {port}")
        return FakeServer(), object()

    async def fake_run(stop: asyncio.Event) -> None:
        events.append("run")

    async def fake_dispose() -> None:
        events.append("dispose")

    monkeypatch.setattr(worker_main, "start_http_server", start_http_server)
    monkeypatch.setattr(worker_main, "run", fake_run)
    _fake_notifications(monkeypatch, events)
    monkeypatch.setattr(worker_main, "dispose_engine", fake_dispose)
    monkeypatch.setattr(worker_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_main, "_stop_on_signals", lambda loop, stop: None)

    await worker_main.main()

    port = get_settings().worker_metrics_port
    assert events == [
        f"server start {port}",
        "run",
        "notifications",
        "bot closed",
        "server shutdown",
        "dispose",
    ]


# ---------------------------------------------------------------- опрос рейсов


class PollScript:
    """Подмена `poll_due_trips`: считает проходы и запоминает, что получила."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.seen: list[tuple[object, object, asyncio.Event | None]] = []

    async def __call__(
        self,
        session_factory: Any,
        provider: Any,
        *,
        stop: asyncio.Event | None = None,
    ) -> None:
        self.calls += 1
        self.seen.append((session_factory, provider, stop))
        if self.error is not None:
            raise self.error


async def _wait_calls(script: PollScript, count: int) -> None:
    for _ in range(300):
        if script.calls >= count:
            return
        await asyncio.sleep(0.01)


async def test_flight_polling_loop_polls_repeatedly_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = PollScript()
    provider: Any = object()
    monkeypatch.setattr(worker, "poll_due_trips", script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run_flight_polling(stop, provider, interval=0.01))

    await _wait_calls(script, 3)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    assert script.seen[0] == (SESSION_FACTORY, provider, stop)


async def test_flight_polling_loop_survives_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    script = PollScript(error=ConnectionRefusedError("db is down"))
    monkeypatch.setattr(worker, "poll_due_trips", script)
    caplog.set_level(logging.ERROR)
    stop = asyncio.Event()
    provider: Any = object()
    task = asyncio.ensure_future(worker.run_flight_polling(stop, provider, interval=0.01))

    await _wait_calls(script, 3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_flight_polling_loop_exits_at_once_when_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = PollScript()
    monkeypatch.setattr(worker, "poll_due_trips", script)
    stop = asyncio.Event()
    stop.set()
    provider: Any = object()

    await asyncio.wait_for(worker.run_flight_polling(stop, provider), TIMEOUT)

    assert script.calls == 0


async def _run_main(monkeypatch: pytest.MonkeyPatch, provider: object | None) -> list[str]:
    events: list[str] = []

    class FakeServer:
        def shutdown(self) -> None:
            pass

    async def fake_run(stop: asyncio.Event) -> None:
        events.append("run")

    async def fake_polling(stop: asyncio.Event, given: object) -> None:
        events.append("polling")
        assert given is provider

    async def fake_dispose() -> None:
        pass

    monkeypatch.setattr(worker_main, "start_http_server", lambda port: (FakeServer(), object()))
    monkeypatch.setattr(worker_main, "run", fake_run)
    _fake_notifications(monkeypatch, events)
    monkeypatch.setattr(worker_main, "run_flight_polling", fake_polling)
    monkeypatch.setattr(worker_main, "build_provider", lambda settings: provider)
    monkeypatch.setattr(worker_main, "dispose_engine", fake_dispose)
    monkeypatch.setattr(worker_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_main, "_stop_on_signals", lambda loop, stop: None)

    await worker_main.main()
    return events


async def test_main_starts_flight_polling_when_provider_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = await _run_main(monkeypatch, object())

    assert sorted(events) == ["bot closed", "notifications", "polling", "run"]


async def test_main_skips_flight_polling_without_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    events = await _run_main(monkeypatch, None)

    assert sorted(events) == ["bot closed", "notifications", "run"]
    assert any(
        r.name == "app.worker" and r.levelno == logging.INFO and "опрос рейсов" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------- отправка уведомлений


class SendScript:
    """Подмена `send_due`: считает проходы, запоминает аргументы, отдаёт заготовленный итог."""

    def __init__(self, result: SendResult | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.result = result or SendResult()
        self.error = error
        self.seen: list[tuple[object, object, asyncio.Event | None]] = []

    async def __call__(
        self,
        session_factory: Any,
        sender: Any,
        *,
        batch: int,
        stop: asyncio.Event | None = None,
    ) -> SendResult:
        self.calls += 1
        self.seen.append((session_factory, sender, stop))
        await asyncio.sleep(0)  # как настоящий проход: отдаёт управление циклу событий
        if self.error is not None:
            raise self.error
        return self.result


async def test_notification_loop_sends_repeatedly_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = SendScript()
    sender: Any = object()
    monkeypatch.setattr(worker, "send_due", script)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run_notification_sender(stop, sender, interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    assert script.seen[0] == (SESSION_FACTORY, sender, stop)


async def test_notification_loop_exits_at_once_when_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = SendScript()
    monkeypatch.setattr(worker, "send_due", script)
    stop = asyncio.Event()
    stop.set()
    sender: Any = object()

    await asyncio.wait_for(worker.run_notification_sender(stop, sender), TIMEOUT)

    assert script.calls == 0


async def test_notification_loop_continues_at_once_after_a_full_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = SendScript(result=SendResult(sent=worker.NOTIFICATION_BATCH))
    monkeypatch.setattr(worker, "send_due", script)
    stop = asyncio.Event()
    sender: Any = object()
    task = asyncio.ensure_future(worker.run_notification_sender(stop, sender, interval=60))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3  # пауза в 60 с не наступила: очередь не пуста


async def test_notification_loop_survives_errors_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    script = SendScript(error=ConnectionRefusedError("secret-detail"))
    monkeypatch.setattr(worker, "send_due", script)
    caplog.set_level(logging.ERROR)
    stop = asyncio.Event()
    sender: Any = object()
    task = asyncio.ensure_future(worker.run_notification_sender(stop, sender, interval=0.01))

    for _ in range(300):
        if script.calls >= 3:
            break
        await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)

    assert script.calls >= 3
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert all("secret-detail" not in r.getMessage() for r in errors)
    assert all("ConnectionRefusedError" in r.getMessage() for r in errors)
