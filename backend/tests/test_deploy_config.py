"""Конфигурация запуска, от которой зависят логи: access-лог uvicorn содержит invite-коды."""

from pathlib import Path
from typing import Any

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROD_COMPOSE = BACKEND_DIR.parent / "deploy" / "docker-compose.prod.yml"
DEV_COMPOSE = BACKEND_DIR.parent / "docker-compose.yml"


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
