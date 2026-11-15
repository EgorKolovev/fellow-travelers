"""ASGI middleware: request_id, метрики и строка access-лога на каждый запрос.

Чистый ASGI, не `BaseHTTPMiddleware`: приложение работает в той же задаче, поэтому `request_id`
в `ContextVar` виден обработчикам и логам, а ответ не буферизуется. Токен `request_id` сбрасывается
в `finally` на любом исходе запроса: значение не переживает запрос и не протекает в следующие
запросы той же asyncio-задачи.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import context
from app.observability.metrics import method_label, observe_request, route_label

REQUEST_ID_HEADER = "X-Request-ID"
# Чужой request_id принимаем только в безопасном виде: он попадает в логи и заголовок ответа.
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")
# Служебные пути: метрики пишем, строку в лог — нет (пробы и сбор метрик идут каждые секунды).
_SERVICE_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

access_logger = logging.getLogger("app.access")


def pick_request_id(scope: Scope) -> str:
    """Берёт X-Request-ID из запроса, если он подходит под шаблон, иначе генерирует новый."""
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            if _REQUEST_ID_PATTERN.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


def route_template(scope: Scope) -> str | None:
    """Шаблон маршрута вместе с префиксами `include_router`, например `/api/v1/invites/{code}`.

    Во FastAPI 0.162 `scope["route"]` — исходный маршрут роутера, его `path` без префикса
    (`/invites/{code}`). Полный шаблон лежит в контексте найденного маршрута; для более старых
    версий, где `route.path` уже полный, остаётся запасной вариант. Не нашлось ничего — `None`.
    """
    effective = scope.get("fastapi", {}).get("effective_route_context")
    path = getattr(effective, "path_format", None)
    if not isinstance(path, str):
        path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str):
        return path
    # /metrics — не APIRoute, в scope его маршрута нет; путь из закрытого набора, id в нём нет
    return scope["path"] if scope["path"] in _SERVICE_PATHS else None


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = pick_request_id(scope)
        token = context.request_id.set(request_id)
        started = time.perf_counter()
        status: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            self._finish(scope, 500, started)
            raise
        else:
            if status is not None:
                self._finish(scope, status, started)
        finally:
            # Сбрасываем токен на любом исходе, включая необработанное исключение: значение
            # не должно доживать до следующего запроса той же asyncio-задачи (ASGITransport
            # в тестах переиспользует задачу, и без сброса туда протекал бы чужой request_id —
            # в том числе подставленный клиентом через X-Request-ID). «Unhandled exception»
            # пишет обработчик ServerErrorMiddleware, который стоит снаружи и вызывается уже
            # после сброса, поэтому в его записи request_id нет; запрос остаётся опознаваем
            # по access-строке со статусом 500.
            context.request_id.reset(token)

    @staticmethod
    def _finish(scope: Scope, status: int, started: float) -> None:
        elapsed = time.perf_counter() - started
        method = scope["method"]
        route = route_template(scope)
        observe_request(method, route, status, elapsed)
        if scope["path"] not in _SERVICE_PATHS:
            access_logger.info(
                "request",
                extra={
                    "method": method_label(method),
                    "route": route_label(route),
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
