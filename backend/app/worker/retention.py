"""Цикл удаления данных (Д11): раз в час удаляет группы с истёкшим сроком хранения."""

import asyncio
import contextlib
import logging

from app.db import get_session_factory
from app.services.retention import purge_expired

logger = logging.getLogger("app.worker")

RETENTION_INTERVAL_SECONDS = 3600.0


async def run_retention(
    stop: asyncio.Event, *, interval: float = RETENTION_INTERVAL_SECONDS
) -> None:
    """Вызывает `purge_expired` раз в `interval` секунд, пока `stop` не выставлен.

    Ошибка прохода пишется в лог, цикл продолжается после паузы. Запускается воркером только
    при `RETENTION_ENABLED`.
    """
    session_factory = get_session_factory()
    while not stop.is_set():
        try:
            await purge_expired(session_factory)
        except Exception:
            logger.exception("Ошибка цикла удаления данных, повтор после паузы")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
