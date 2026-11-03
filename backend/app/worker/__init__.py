"""Воркер фоновых заданий: цикл опроса очереди `background_jobs` (`python -m app.worker`)."""

import asyncio
import contextlib
import logging

from app.db import get_session_factory
from app.services.jobs import process_next

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0


async def run(stop: asyncio.Event, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """Выполняет готовые задания, пока `stop` не выставлен; между опросами ждёт `poll_interval`.

    За один опрос берёт задания одно за другим, пока очередь не опустеет. Задание, взятое до
    `stop`, доводится до конца: останавливается только сам цикл. Любая ошибка цикла (БД
    недоступна, таблицы ещё нет — миграции в проде делает `api`, а воркер может стартовать
    раньше) пишется в лог, после паузы цикл продолжается.
    """
    session_factory = get_session_factory()
    while not stop.is_set():
        try:
            while not stop.is_set() and await process_next(session_factory) is not None:
                pass
        except Exception:
            logger.exception("Ошибка цикла воркера, повтор после паузы")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
