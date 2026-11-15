"""Конфигурация запуска и поставки: ловит правки, которые молча ломают воспроизводимость и логи.

Access-лог uvicorn содержит invite-коды; образы и пакеты должны ставиться с точными версиями и
хешами; наружу через nginx не проксируется ничего, кроме `/api/`.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROD_COMPOSE = BACKEND_DIR.parent / "deploy" / "docker-compose.prod.yml"
REPO_DIR = BACKEND_DIR.parent
DEV_COMPOSE = REPO_DIR / "docker-compose.yml"
DOCKERFILES = (
    BACKEND_DIR / "Dockerfile",
    REPO_DIR / "frontend" / "Dockerfile",
    REPO_DIR / "deploy" / "caddy" / "Dockerfile",
)
WORKFLOWS = tuple(sorted((REPO_DIR / ".github" / "workflows").glob("*.yml")))
NGINX_CONF = REPO_DIR / "frontend" / "nginx.conf"

# сколько числовых компонент версии образа считается точной: у postgres версия «16.15»
EXACT_VERSION_PARTS = {"postgres": 2}
DEFAULT_VERSION_PARTS = 3


def test_dockerfile_disables_uvicorn_access_log() -> None:
    cmd = [
        line
        for line in (BACKEND_DIR / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD")
    ]

    assert len(cmd) == 1
    assert '"uvicorn"' in cmd[0]
    assert '"--no-access-log"' in cmd[0]


def test_prod_compose_disables_uvicorn_access_log() -> None:
    compose: dict[str, Any] = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    command = compose["services"]["api"]["command"]

    assert "uvicorn app.main:app" in command
    assert "--no-access-log" in command


@pytest.mark.parametrize("path", [DEV_COMPOSE, PROD_COMPOSE], ids=["dev", "prod"])
def test_worker_has_healthcheck_and_no_published_ports(path: Path) -> None:
    compose: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    worker = compose["services"]["worker"]

    # порт метрик читают из сети compose: наружу воркер ничего не публикует
    assert "ports" not in worker
    assert worker["healthcheck"] == {
        "test": ["CMD", "python", "-m", "app.worker.healthcheck"],
        "interval": "30s",
        "timeout": "5s",
        "retries": 3,
        "start_period": "20s",
    }


@pytest.mark.parametrize(
    ("path", "service", "with_metrics_port"),
    [
        (DEV_COMPOSE, "api", True),
        (DEV_COMPOSE, "worker", True),
        (DEV_COMPOSE, "bot", True),
        (PROD_COMPOSE, "api", True),
        (PROD_COMPOSE, "worker", True),
        (PROD_COMPOSE, "bot", False),
    ],
    ids=["dev-api", "dev-worker", "dev-bot", "prod-api", "prod-worker", "prod-bot"],
)
def test_log_level_and_worker_metrics_port_reach_container_environment(
    path: Path, service: str, with_metrics_port: bool
) -> None:
    compose: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    environment = compose["services"][service]["environment"]

    # без проводки в environment контейнера их установка в .env на сервере ничего не делает;
    # дефолты (`INFO`, `9100`) повторяют дефолты Settings в app/config.py
    assert environment["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}"
    if with_metrics_port:
        assert environment["WORKER_METRICS_PORT"] == "${WORKER_METRICS_PORT:-9100}"
    else:
        # у бота нет metrics-сервера: WORKER_METRICS_PORT ему не нужен
        assert "WORKER_METRICS_PORT" not in environment


# ---------------------------------------------------------------- Dockerfile, образы, пакеты


def _dockerfile_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def test_backend_image_installs_locked_requirements_before_copying_app() -> None:
    lines = _dockerfile_lines(DOCKERFILES[0])

    copy_lock = next(i for i, line in enumerate(lines) if line.startswith("COPY requirements.lock"))
    install = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("RUN") and "pip install" in line and "requirements.lock" in line
    )
    copy_app = next(i for i, line in enumerate(lines) if line.startswith("COPY app "))

    # зависимости отдельным слоем: правка кода не переустанавливает пакеты
    assert copy_lock < install < copy_app
    assert "--require-hashes" in lines[install]
    assert "--no-deps" in lines[install]


def _is_exact(image: str) -> bool:
    name, _, tag = image.partition(":")
    if not tag or "@sha256:" in image:
        return "@sha256:" in image  # без тега и без дайджеста версия не зафиксирована
    base = name.rsplit("/", 1)[-1]
    match = re.match(r"\d+(?:\.\d+)*", tag)
    if match is None:
        return False  # latest, local, stable и подобное
    parts = match.group(0).count(".") + 1
    return parts >= EXACT_VERSION_PARTS.get(base, DEFAULT_VERSION_PARTS)


@pytest.mark.parametrize(
    ("image", "exact"),
    [
        ("python:3.12.16-slim", True),
        ("node:24.22.2-alpine", True),
        ("postgres:16.15-alpine", True),
        ("caddy:2.11.7-alpine", True),
        ("postgres:16-alpine", False),
        ("python:3.12-slim", False),
        ("nginx:latest", False),
        ("nginx", False),
        ("caddy:alpine", False),
        ("app:local", False),
    ],
)
def test_exact_version_rule(image: str, exact: bool) -> None:
    assert _is_exact(image) is exact


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_DIR)))
def test_every_dockerfile_base_image_has_an_exact_version(path: Path) -> None:
    images = [
        line.split()[-1] if " AS " not in line.upper() else line.split()[1]
        for line in _dockerfile_lines(path)
        if line.upper().startswith("FROM ")
    ]

    assert images
    for image in images:
        assert _is_exact(image), f"{path.name}: {image} без точной версии"


@pytest.mark.parametrize("path", [DEV_COMPOSE, PROD_COMPOSE], ids=["dev", "prod"])
def test_every_compose_image_has_an_exact_version(path: Path) -> None:
    compose: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    checked = []

    for name, service in compose["services"].items():
        image = service.get("image")
        # образ, который собирается из исходников, получает локальный тег; ${...} подставляет деплой
        if image is None or "build" in service or "${" in image:
            continue
        assert _is_exact(image), f"{path.name}: {name} использует {image}"
        checked.append(name)

    assert "db" in checked


PIP_INSTALL = re.compile(r"""\bpip\d*["']?\s+install\b""")


def _shell_commands(workflow: dict[str, Any]) -> list[str]:
    """Команды шагов `run`: строки с продолжением через обратную косую склеены."""
    commands = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            text = step.get("run")
            if text:
                commands.extend(re.sub(r"\\\n\s*", " ", text).splitlines())
    return commands


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_install_pip_packages_only_with_hashes(path: Path) -> None:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    pip_installs = [c for c in _shell_commands(workflow) if PIP_INSTALL.search(c)]

    for command in pip_installs:
        assert "--require-hashes" in command, command


def test_deploy_installs_its_tooling_with_hashes() -> None:
    workflow: dict[str, Any] = yaml.safe_load(
        (REPO_DIR / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    )

    pip_installs = [c for c in _shell_commands(workflow) if PIP_INSTALL.search(c)]

    assert pip_installs
    assert all("--require-hashes" in c and "-r " in c for c in pip_installs)


# ---------------------------------------------------------------- nginx


_PASS_DIRECTIVES = frozenset({"proxy_pass", "fastcgi_pass", "uwsgi_pass", "scgi_pass", "grpc_pass"})


def _proxy_locations(config: str) -> dict[str | None, list[str]]:
    """Директивы `*_pass` по location, в котором стоят (None — вне location)."""
    text = re.sub(r"#[^\n]*", "", config)
    result: dict[str | None, list[str]] = {}
    blocks: list[list[str]] = []  # заголовки открытых блоков
    statement: list[str] = []
    for token in re.findall(r"[{};]|[^\s{};]+", text):
        if token == "{":
            blocks.append(statement)
            statement = []
        elif token == "}":
            blocks.pop()
            statement = []
        elif token == ";":
            if statement and statement[0] in _PASS_DIRECTIVES:
                headers = [b for b in blocks if b and b[0] == "location"]
                location = headers[-1][-1] if headers else None
                result.setdefault(location, []).append(" ".join(statement))
            statement = []
        else:
            statement.append(token)
    return result


def test_nginx_proxies_only_api() -> None:
    passes = _proxy_locations(NGINX_CONF.read_text(encoding="utf-8"))

    # служебные /metrics, /readyz и /healthz остаются внутри сети: наружу идёт только /api/
    assert list(passes) == ["/api/"]


def test_proxy_location_parser_flags_other_locations() -> None:
    config = """
    server {
        location /api/ { proxy_pass http://api:8000; }
        location /metrics { proxy_pass http://api:8000; }
        location / { try_files $uri /index.html; }
    }
    """

    assert sorted(k for k in _proxy_locations(config) if k is not None) == ["/api/", "/metrics"]


@pytest.mark.parametrize(
    "command",
    [
        "pip install -r requirements.txt",
        'pip3 install --quiet "$RUNNER_TEMP/x"',
        '"$RUNNER_TEMP/venv/bin/pip" install --quiet -r r.txt',
        "python -m pip install requests",
    ],
)
def test_pip_install_matcher_recognises_common_spellings(command: str) -> None:
    assert PIP_INSTALL.search(command)
