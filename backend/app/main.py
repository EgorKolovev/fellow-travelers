import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import Route

from app.api.deps import SessionDep
from app.api.v1 import router as v1_router
from app.config import get_settings
from app.db import dispose_engine
from app.errors import register_error_handlers
from app.observability.logging import configure_logging
from app.observability.metrics import MetricsEndpoint
from app.observability.middleware import ObservabilityMiddleware

logger = logging.getLogger(__name__)

# проба не должна висеть, пока БД не отвечает
READYZ_TIMEOUT_SECONDS = 3


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Fellow Travelers API", lifespan=lifespan)
    register_error_handlers(app)
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(v1_router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(session: SessionDep) -> JSONResponse:
        """Приложение готово принимать запросы: есть связь с БД."""
        try:
            async with asyncio.timeout(READYZ_TIMEOUT_SECONDS):
                await session.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            # TimeoutError — подкласс OSError; отказ БД пробы не должен превращаться в 500
            logger.warning("readiness check failed", exc_info=True)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # служебный путь в корне, вне контракта и не под /api, как /healthz
    app.router.routes.append(Route("/metrics", MetricsEndpoint(), methods=["GET"]))

    return app


app = create_app()
