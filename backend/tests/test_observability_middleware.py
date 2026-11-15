import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from starlette.types import Message, Receive, Scope, Send

from app.observability import context
from app.observability.middleware import (
    REQUEST_ID_HEADER,
    ObservabilityMiddleware,
    route_template,
)
from tests.logs import JsonLogs

VALID_ID = "req-12345678"
UUID_HEX = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    @app.get("/probe/{item_id}")
    async def probe(item_id: str) -> dict[str, str | None]:
        return {"request_id": context.request_id.get()}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret")

    return app


def requests_count(route: str, status: str = "200", method: str = "GET") -> float:
    labels = {"method": method, "route": route, "status": status}
    return REGISTRY.get_sample_value("ft_http_requests_total", labels) or 0.0


def durations_count(route: str, method: str = "GET") -> float:
    labels = {"method": method, "route": route}
    return REGISTRY.get_sample_value("ft_http_request_duration_seconds_count", labels) or 0.0


def all_request_labels() -> list[dict[str, str]]:
    return [
        dict(sample.labels)
        for metric in REGISTRY.collect()
        if metric.name == "ft_http_requests"
        for sample in metric.samples
    ]


# ---------------------------------------------------------------- request_id


async def test_generates_request_id(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert UUID_HEX.fullmatch(response.headers[REQUEST_ID_HEADER])


async def test_generated_ids_differ(client: AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_accepts_valid_header_and_exposes_it_to_handlers(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/probe/x", headers={REQUEST_ID_HEADER: VALID_ID})

    assert response.headers[REQUEST_ID_HEADER] == VALID_ID
    assert response.json() == {"request_id": VALID_ID}


@pytest.mark.parametrize(
    "value", ["a" * 8, "a" * 64, "Ab-09-Zz", "3f2504e0-4f89-41d3-9a0c-0305e82c3301"]
)
async def test_accepts_boundary_and_uuid_values(client: AsyncClient, value: str) -> None:
    response = await client.get("/healthz", headers={REQUEST_ID_HEADER: value})

    assert response.headers[REQUEST_ID_HEADER] == value


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"short",
        b"a" * 7,
        b"a" * 65,
        b"has space 1234",
        b"under_score_1234",
        b"semi;colon-1234",
        b"<script>alert1</script>",
        "ёжик-ёжик-ёжик".encode(),
    ],
)
async def test_invalid_header_is_replaced(client: AsyncClient, value: bytes) -> None:
    response = await client.get("/healthz", headers={REQUEST_ID_HEADER.encode(): value})

    issued = response.headers[REQUEST_ID_HEADER]
    assert UUID_HEX.fullmatch(issued)
    assert issued.encode() != value


async def test_request_id_does_not_leak_into_next_context(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    await client.get("/probe/x", headers={REQUEST_ID_HEADER: VALID_ID})

    assert context.request_id.get() is None


async def test_request_id_does_not_leak_after_unhandled_exception(
    probe_app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    # ASGITransport выполняет оба запроса в одной asyncio-задаче, то есть в общем контексте:
    # падение первого запроса не должно оставлять в нём свой (подставленный клиентом) request_id
    await client.get("/boom", headers={REQUEST_ID_HEADER: VALID_ID})

    response = await client.get("/probe/x")
    fresh = response.json()["request_id"]

    assert UUID_HEX.fullmatch(fresh)
    assert fresh != VALID_ID
    [probe_log] = [r for r in json_logs.by_logger("app.access") if r["route"] == "/probe/{item_id}"]
    assert probe_log["request_id"] == fresh
    assert context.request_id.get() is None


async def test_error_response_keeps_request_id_header(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/nothing-here", headers={REQUEST_ID_HEADER: VALID_ID})

    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == VALID_ID


# ---------------------------------------------------------------- маршрут из scope


async def test_route_template_is_available_in_scope_after_routing(app: FastAPI) -> None:
    seen: dict[str, Any] = {}

    async def spy(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)
        seen["route"] = scope.get("route")
        seen["template"] = route_template(scope)

    async with AsyncClient(transport=ASGITransport(app=spy), base_url="http://test") as client:
        # без авторизации: ответ 401 приходит раньше, чем зависимости доберутся до БД
        response = await client.get("/api/v1/invites/QZX742")

    assert response.status_code == 401
    # маршрут роутер в scope кладёт, но во FastAPI 0.162 его path без префикса /api/v1
    assert seen["route"].path.endswith("/invites/{code}")
    assert seen["template"] == "/api/v1/invites/{code}"


def test_route_template_falls_back_to_route_path() -> None:
    scope: Scope = {"path": "/items/5", "route": SimpleNamespace(path="/items/{item_id}")}

    assert route_template(scope) == "/items/{item_id}"


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_route_template_of_service_path_without_route(path: str) -> None:
    assert route_template({"path": path}) == path


@pytest.mark.parametrize("path", ["/", "/api/v1/invites/QZX742", "/metrics/", "/unknown"])
def test_route_template_is_none_for_unmatched_path(path: str) -> None:
    assert route_template({"path": path}) is None


# ---------------------------------------------------------------- метрики


async def test_metrics_use_route_template_not_real_path(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    before = (requests_count("/probe/{item_id}"), durations_count("/probe/{item_id}"))

    await client.get("/probe/secret-aaa111")
    await client.get("/probe/secret-bbb222")

    assert requests_count("/probe/{item_id}") == before[0] + 2
    assert durations_count("/probe/{item_id}") == before[1] + 2
    rendered = " ".join(" ".join(labels.values()) for labels in all_request_labels())
    assert "secret-aaa111" not in rendered
    assert "secret-bbb222" not in rendered


async def test_invite_code_never_reaches_labels(client: AsyncClient) -> None:
    route = "/api/v1/invites/{code}"
    before = requests_count(route, status="401")

    await client.get("/api/v1/invites/QZX742")

    assert requests_count(route, status="401") == before + 1
    assert "QZX742" not in " ".join(" ".join(labels.values()) for labels in all_request_labels())


async def test_unmatched_paths_share_one_label(client: AsyncClient) -> None:
    before = requests_count("unmatched", status="404")

    for suffix in ("a1b2c3d4", "e5f6a7b8", "c9d0e1f2"):
        response = await client.get(f"/no/such/{suffix}")
        assert response.status_code == 404

    assert requests_count("unmatched", status="404") == before + 3
    rendered = " ".join(" ".join(labels.values()) for labels in all_request_labels())
    assert "a1b2c3d4" not in rendered


async def test_unknown_method_is_other(client: AsyncClient) -> None:
    before = requests_count("/healthz", status="405", method="OTHER")

    response = await client.request("PROPFIND", "/healthz")

    assert response.status_code == 405
    assert requests_count("/healthz", status="405", method="OTHER") == before + 1
    assert "PROPFIND" not in " ".join(" ".join(labels.values()) for labels in all_request_labels())


async def test_service_paths_are_counted(client: AsyncClient) -> None:
    before_health = requests_count("/healthz")
    before_metrics = requests_count("/metrics")

    await client.get("/healthz")
    await client.get("/metrics")

    assert requests_count("/healthz") == before_health + 1
    assert requests_count("/metrics") == before_metrics + 1


# ---------------------------------------------------------------- access-лог


async def test_access_log_line(
    probe_app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    await client.get("/probe/secret-aaa111", headers={REQUEST_ID_HEADER: VALID_ID})

    [record] = json_logs.by_logger("app.access")
    assert record["message"] == "request"
    assert record["level"] == "INFO"
    assert record["method"] == "GET"
    assert record["route"] == "/probe/{item_id}"
    assert record["status"] == 200
    assert isinstance(record["duration_ms"], int | float)
    assert record["duration_ms"] >= 0
    assert record["request_id"] == VALID_ID
    assert "secret-aaa111" not in json_logs.text


async def test_unmatched_route_in_log(client: AsyncClient, json_logs: JsonLogs) -> None:
    await client.get("/no/such/a1b2c3d4")

    [record] = json_logs.by_logger("app.access")
    assert record["route"] == "unmatched"
    assert record["status"] == 404
    assert "a1b2c3d4" not in json_logs.text


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
async def test_service_paths_are_not_logged(
    client: AsyncClient, json_logs: JsonLogs, path: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert json_logs.by_logger("app.access") == []


# ---------------------------------------------------------------- необработанное исключение


async def test_unhandled_exception_is_counted_as_500_and_propagated(
    probe_app: FastAPI, client: AsyncClient, json_logs: JsonLogs
) -> None:
    before = (requests_count("/boom", status="500"), durations_count("/boom"))

    response = await client.get("/boom", headers={REQUEST_ID_HEADER: VALID_ID})

    # ответ отдаёт обработчик из app/errors.py: исключение дошло до ServerErrorMiddleware
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert requests_count("/boom", status="500") == before[0] + 1
    assert durations_count("/boom") == before[1] + 1
    [access] = json_logs.by_logger("app.access")
    assert access["status"] == 500
    assert access["route"] == "/boom"
    assert access["request_id"] == VALID_ID
    [unhandled] = json_logs.by_logger("app.errors")
    assert unhandled["message"] == "Unhandled exception"
    assert unhandled["exc_type"] == "RuntimeError"
    # токен сбрасывается в finally до того, как ServerErrorMiddleware (снаружи middleware)
    # логирует исключение, поэтому request_id живёт только внутри обработки запроса
    assert "request_id" not in unhandled


async def test_exception_propagates_out_of_middleware() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    scope: Scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise AssertionError("ответ не должен отправляться")

    with pytest.raises(RuntimeError, match="boom"):
        await ObservabilityMiddleware(failing)(scope, receive, send)


async def test_non_http_scopes_pass_through() -> None:
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["type"])

    async def receive() -> Message:
        return {}

    async def send(message: Message) -> None:
        return None

    middleware = ObservabilityMiddleware(inner)
    await middleware({"type": "lifespan"}, receive, send)
    await middleware({"type": "websocket"}, receive, send)

    assert calls == ["lifespan", "websocket"]
    assert context.request_id.get() is None
