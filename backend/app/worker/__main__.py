import asyncio
import logging
import signal

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
    configure_logging(get_settings().log_level)
    stop = asyncio.Event()
    _stop_on_signals(asyncio.get_running_loop(), stop)
    logger.info("Воркер запущен")
    try:
        await run(stop)
    finally:
        await dispose_engine()
        logger.info("Воркер остановлен")


if __name__ == "__main__":
    asyncio.run(main())
