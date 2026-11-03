"""Очередь фоновых заданий: постановка в транзакции правки и выполнение одним заданием за раз."""

import logging
import uuid
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BackgroundJob, JobKind, JobStatus
from app.observability import context
from app.services.matching import lock_event, reconcile_event

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8

_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 900
_LAST_ERROR_LENGTH = 128


class JobOutcome(StrEnum):
    """Чем кончилась попытка выполнить задание."""

    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"


def retry_delay(attempts: int) -> timedelta:
    """Пауза до повтора: `min(5 · 2^attempts, 900)` секунд.

    `attempts` — сколько неудачных попыток уже было до текущей: после первой ошибки ждём
    `retry_delay(0)` = 5 с, после второй — 10 с и так далее, не дольше 15 минут.
    """
    return timedelta(seconds=min(_RETRY_BASE_SECONDS * 2**attempts, _RETRY_MAX_SECONDS))


def enqueue_reconcile(session: AsyncSession, *, event_id: uuid.UUID, trip_id: uuid.UUID) -> None:
    """Ставит пересчёт трансферов группы после правки рейса: только `session.add`.

    Задание живёт в транзакции вызывающего: откатилась правка — нет и задания, прошёл commit —
    задание не потеряется.
    """
    session.add(BackgroundJob(event_id=event_id, kind=JobKind.RECONCILE_TRIP, trip_id=trip_id))


async def _register_failure(
    session: AsyncSession, job: BackgroundJob, exc: Exception
) -> JobOutcome:
    """Учитывает неудачную попытку: счётчик, пауза до повтора или `failed`; пишет лог и commit."""
    attempts = job.attempts + 1
    values: dict[str, object] = {
        "attempts": attempts,
        "last_error": type(exc).__name__[:_LAST_ERROR_LENGTH],
    }
    outcome = JobOutcome.FAILED if attempts >= MAX_ATTEMPTS else JobOutcome.RETRY
    if outcome == JobOutcome.FAILED:
        values["status"] = JobStatus.FAILED
    else:
        values["run_after"] = func.now() + retry_delay(job.attempts)
    kind, job_id = job.kind.value, job.id
    await session.execute(update(BackgroundJob).where(BackgroundJob.id == job_id).values(**values))
    await session.commit()
    # в сообщении нет текста исключения: он может содержать персональные данные
    extra = {"kind": kind, "outcome": outcome.value, "attempts": attempts}
    if outcome == JobOutcome.FAILED:
        logger.error("Задание не выполнено, повторов больше нет", extra=extra, exc_info=exc)
    else:
        logger.warning("Задание не выполнено, будет повтор", extra=extra, exc_info=exc)
    return outcome


async def process_next(session_factory: async_sessionmaker[AsyncSession]) -> JobOutcome | None:
    """Забирает одно готовое задание и выполняет его в одной транзакции; None, если заданий нет.

    Задание блокируется `FOR UPDATE SKIP LOCKED`, поэтому несколько воркеров делят очередь, не
    ожидая друг друга. Пересчёт идёт внутри savepoint, под `lock_event` группы. Успех удаляет
    задание в той же транзакции: повторная доставка ничего не меняет. Ошибка откатывает только
    пересчёт: `attempts` растёт, `run_after` сдвигается на `retry_delay`, в `last_error`
    пишется имя класса исключения (текст может содержать персональные данные). После
    `MAX_ATTEMPTS` попыток задание получает `failed` и больше не берётся.
    """
    async with session_factory() as session:
        job = (
            await session.execute(
                select(BackgroundJob)
                .where(
                    BackgroundJob.status == JobStatus.PENDING,
                    BackgroundJob.run_after <= func.now(),
                )
                .order_by(BackgroundJob.run_after, BackgroundJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        job_id, kind = job.id, job.kind.value
        token = context.job_id.set(job_id)
        try:
            try:
                async with session.begin_nested():
                    await lock_event(session, job.event_id)
                    result = await reconcile_event(
                        session,
                        job.event_id,
                        changed_trip_ids=(
                            frozenset() if job.trip_id is None else frozenset({job.trip_id})
                        ),
                    )
            except Exception as exc:
                return await _register_failure(session, job, exc)
            await session.execute(delete(BackgroundJob).where(BackgroundJob.id == job_id))
            await session.commit()
            logger.info(
                "Пересчёт трансферов выполнен: исключено %d, расформировано %d, размещено %d",
                result.excluded,
                result.dissolved,
                result.placed,
                extra={"kind": kind, "outcome": JobOutcome.DONE.value},
            )
            return JobOutcome.DONE
        finally:
            context.job_id.reset(token)
