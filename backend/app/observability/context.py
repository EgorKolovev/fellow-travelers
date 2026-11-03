"""Контекст текущей операции — запроса API или задания воркера — для логов и аудита."""

import uuid
from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id: ContextVar[uuid.UUID | None] = ContextVar("job_id", default=None)
