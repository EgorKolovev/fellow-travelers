"""Метрики Prometheus. Лейблы берутся только из закрытых множеств.

Идентификаторов и значений из пути (invite-код, uuid) в лейблах нет: `route` — шаблон маршрута,
`method` — из списка стандартных методов, `status` — число, остальные лейблы — значения
закрытых `StrEnum` (триггер и вид размещения ниже, `AuditReason`, `JobKind`, `JobOutcome`,
`JobStatus`). Иначе число рядов росло бы без границы.

Счётчики подбора и воркера обновляются после commit (см. `app.observability.deferred`), поэтому
откатившийся сценарий их не меняет.
"""

from enum import StrEnum

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from starlette.types import Receive, Scope, Send

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
OTHER_METHOD = "OTHER"
UNMATCHED_ROUTE = "unmatched"

http_requests_total = Counter(
    "ft_http_requests_total",
    "Обработанные HTTP-запросы",
    ["method", "route", "status"],
)
http_request_duration_seconds = Histogram(
    "ft_http_request_duration_seconds",
    "Длительность обработки HTTP-запроса, секунды",
    ["method", "route"],
)


class MatchTrigger(StrEnum):
    """Что запустило размещение заявки: подача или возврат в поиск, выход другого, пересчёт."""

    APPLICATION = "application"
    LEAVE = "leave"
    RECONCILE = "reconcile"


class PlacementKind(StrEnum):
    """Чем кончилось размещение: дополнили трансфер, собрали пару или тройку, заявка ждёт."""

    JOIN = "join"
    NEW_PAIR = "new_pair"
    NEW_TRIPLE = "new_triple"
    NONE = "none"


match_placements_total = Counter(
    "ft_match_placements_total",
    "Размещения заявок: по триггеру и результату",
    ["trigger", "kind"],
)
match_duration_seconds = Histogram(
    "ft_match_duration_seconds",
    "Длительность одного размещения заявки (match_application), секунды",
    ["trigger"],
)
transfer_exclusions_total = Counter(
    "ft_transfer_exclusions_total",
    "Заявки, вышедшие из трансфера, по причине из истории (AuditReason)",
    ["reason"],
)
transfers_dissolved_total = Counter(
    "ft_transfers_dissolved_total",
    "Расформированные трансферы по причине выхода, с которого всё началось (AuditReason)",
    ["reason"],
)
worker_jobs_total = Counter(
    "ft_worker_jobs_total",
    "Попытки выполнить фоновое задание по виду и исходу",
    ["kind", "outcome"],
)
worker_job_duration_seconds = Histogram(
    "ft_worker_job_duration_seconds",
    "Длительность попытки выполнить фоновое задание, секунды",
    ["kind"],
)
worker_queue_depth = Gauge(
    "ft_worker_queue_depth",
    "Задания в очереди по статусу",
    ["status"],
)
worker_oldest_job_age_seconds = Gauge(
    "ft_worker_oldest_job_age_seconds",
    "Возраст самого старого задания в статусе pending, секунды (0, если очередь пуста)",
)
worker_last_poll_timestamp_seconds = Gauge(
    "ft_worker_last_poll_timestamp_seconds",
    "Unix-время последнего опроса очереди воркером",
)


def method_label(method: str) -> str:
    """Метод из закрытого множества, иначе `OTHER`: клиент может прислать любую строку."""
    return method if method in HTTP_METHODS else OTHER_METHOD


def route_label(route_path: str | None) -> str:
    """Шаблон маршрута (`/api/v1/invites/{code}`) или `unmatched`, если маршрут не нашёлся."""
    return route_path or UNMATCHED_ROUTE


def observe_request(method: str, route_path: str | None, status: int, seconds: float) -> None:
    """Учитывает один завершённый запрос в счётчике и гистограмме."""
    method_value = method_label(method)
    route_value = route_label(route_path)
    http_requests_total.labels(method_value, route_value, str(status)).inc()
    http_request_duration_seconds.labels(method_value, route_value).observe(seconds)


class MetricsEndpoint:
    """ASGI-приложение для `/metrics` на `prometheus_client.make_asgi_app()`.

    Объект с `__call__`, а не функция: Starlette оборачивает функцию-эндпоинт в обработчик запроса,
    а ASGI-приложению нужны `(scope, receive, send)`. `app.mount("/metrics", ...)` не подходит:
    на «/metrics» без слэша он отвечает редиректом 307 на «/metrics/», а `app.add_route` с
    prometheus-client 0.26.0 ломается по той же причине (`make_asgi_app` отдаёт функцию).
    """

    def __init__(self) -> None:
        self._app = make_asgi_app()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)
