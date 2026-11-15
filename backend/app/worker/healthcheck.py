"""Healthcheck воркера: `python -m app.worker.healthcheck`, код 0 — воркер жив.

Читает метрики воркера на 127.0.0.1 и проверяет `ft_worker_last_poll_timestamp_seconds`: опрос
очереди должен быть свежим. Ответ HTTP сам по себе ничего не доказывает (метрики отдаёт отдельный
поток), а свежий опрос показывает, что цикл работает и достаёт до БД.
"""

import sys
import time
import urllib.request

from prometheus_client.parser import text_string_to_metric_families

from app.config import get_settings

LAST_POLL_METRIC = "ft_worker_last_poll_timestamp_seconds"
MAX_POLL_AGE_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 3.0


def last_poll_age(port: int, *, now: float | None = None) -> float | None:
    """Сколько секунд назад был последний опрос; None, если метрик нет или опроса не было."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics", timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            text = response.read().decode()
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                # 0 — значение до первого опроса
                if sample.name == LAST_POLL_METRIC and sample.value > 0:
                    return (time.time() if now is None else now) - sample.value
    except (OSError, ValueError):
        return None
    return None


def is_healthy(port: int, *, now: float | None = None) -> bool:
    age = last_poll_age(port, now=now)
    return age is not None and age < MAX_POLL_AGE_SECONDS


def main() -> int:
    return 0 if is_healthy(get_settings().worker_metrics_port) else 1


if __name__ == "__main__":
    sys.exit(main())
