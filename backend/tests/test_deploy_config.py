"""Конфигурация запуска, от которой зависят логи: access-лог uvicorn содержит invite-коды."""

from pathlib import Path
from typing import Any

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROD_COMPOSE = BACKEND_DIR.parent / "deploy" / "docker-compose.prod.yml"


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
