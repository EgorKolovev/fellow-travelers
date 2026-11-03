"""Метрики Prometheus. Лейблы берутся только из закрытых множеств.

Идентификаторов и значений из пути (invite-код, uuid) в лейблах нет: `route` — шаблон маршрута,
`method` — из списка стандартных методов, `status` — число. Иначе число рядов росло бы без границы.
"""

from prometheus_client import Counter, Histogram, make_asgi_app
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
