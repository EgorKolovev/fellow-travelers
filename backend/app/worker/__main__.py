import asyncio
import logging
import signal

from prometheus_client import start_http_server

from app.config import get_settings
from app.db import dispose_engine
from app.observability.logging import configure_logging
from app.worker import run

logger = logging.getLogger("app.worker")


def _stop_on_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGTERM и SIGINT только ставят `stop`: текущая транзакция заканчивается как обычно."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: цикл событий не умеет обработчики сигналов
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = asyncio.Event()
    _stop_on_signals(asyncio.get_running_loop(), stop)
    # метрики отдаёт отдельный поток; порт не публикуется наружу, его читают из сети compose
    metrics_server, _ = start_http_server(settings.worker_metrics_port)
    logger.info("Воркер запущен, метрики на порту %d", settings.worker_metrics_port)
    try:
        await run(stop)
    finally:
        metrics_server.shutdown()
        await dispose_engine()
        logger.info("Воркер остановлен")


if __name__ == "__main__":
    asyncio.run(main())
