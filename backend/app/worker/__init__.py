"""Воркер фоновых заданий: цикл опроса очереди `background_jobs` (`python -m app.worker`)."""

import asyncio
import contextlib
import logging
import time

from app.db import get_session_factory
from app.flights.provider import FlightProvider
from app.observability import metrics
from app.services.flight_polling import poll_due_trips
from app.services.jobs import observe_queue, process_next
from app.services.notification_sender import NotificationSender, send_due

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
FLIGHT_POLL_INTERVAL_SECONDS = 30.0
NOTIFICATION_INTERVAL_SECONDS = 5.0
NOTIFICATION_BATCH = 20


async def run(stop: asyncio.Event, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """Выполняет готовые задания, пока `stop` не выставлен; между опросами ждёт `poll_interval`.

    Каждый опрос начинается со снимка очереди: gauge'и глубины и возраста старейшего задания, затем
    `ft_worker_last_poll_timestamp_seconds` (по нему healthcheck судит, что цикл жив и видит БД).
    За один опрос берёт задания одно за другим, пока очередь не опустеет. Задание, взятое до
    `stop`, доводится до конца: останавливается только сам цикл. Любая ошибка цикла (БД
    недоступна, таблицы ещё нет — миграции в проде делает `api`, а воркер может стартовать
    раньше) пишется в лог, после паузы цикл продолжается.
    """
    session_factory = get_session_factory()
    while not stop.is_set():
        try:
            await observe_queue(session_factory)
            metrics.worker_last_poll_timestamp_seconds.set(time.time())
            while not stop.is_set() and await process_next(session_factory) is not None:
                pass
        except Exception:
            logger.exception("Ошибка цикла воркера, повтор после паузы")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)


async def run_flight_polling(
    stop: asyncio.Event,
    provider: FlightProvider,
    *,
    interval: float = FLIGHT_POLL_INTERVAL_SECONDS,
) -> None:
    """Опрашивает поставщика рейсов раз в `interval` секунд, пока `stop` не выставлен.

    Идёт рядом с `run`, с тем же `stop`: проход, начатый до остановки, заканчивает текущий рейс
    и не берёт следующие. Ошибка прохода пишется в лог, цикл продолжается после паузы.
    """
    session_factory = get_session_factory()
    while not stop.is_set():
        try:
            await poll_due_trips(session_factory, provider, stop=stop)
        except Exception:
            logger.exception("Ошибка цикла опроса рейсов, повтор после паузы")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run_notification_sender(
    stop: asyncio.Event,
    sender: NotificationSender,
    *,
    interval: float = NOTIFICATION_INTERVAL_SECONDS,
) -> None:
    """Отправляет уведомления из `notifications` раз в `interval` секунд, пока `stop` не выставлен.

    Полный проход (взяли `NOTIFICATION_BATCH` строк) повторяется сразу, неполный — после паузы.
    Проход, начатый до остановки, дозавершает текущую отправку и не берёт следующие. Ошибка
    прохода пишется в лог (без текста сообщений), цикл продолжается после паузы.
    """
    session_factory = get_session_factory()
    while not stop.is_set():
        taken = 0
        try:
            result = await send_due(session_factory, sender, batch=NOTIFICATION_BATCH, stop=stop)
            taken = result.sent + result.skipped + result.retried + result.failed
        except Exception as exc:
            logger.error(
                "Ошибка цикла отправки уведомлений (%s), повтор после паузы", type(exc).__name__
            )
        if taken >= NOTIFICATION_BATCH:
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
