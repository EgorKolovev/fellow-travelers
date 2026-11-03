"""Очередь фоновых заданий: постановка в транзакции правки и выполнение одним заданием за раз."""

import logging
import time
import uuid
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import DateTime, delete, extract, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BackgroundJob, JobKind, JobStatus
from app.observability import context, metrics
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


async def enqueue_reconcile(
    session: AsyncSession, *, event_id: uuid.UUID, trip_id: uuid.UUID
) -> None:
    """Ставит пересчёт трансферов группы после правки рейса; без commit.

    Вызывать под `lock_event` группы, в транзакции правки: откатилась правка — нет и задания,
    прошёл commit — задание не потеряется. Ожидающее задание на группу одно (уникальный частичный
    индекс): если оно уже есть, `ON CONFLICT DO NOTHING` ничего не делает, и новая правка сливается
    с ним. Пересчёт читает состояние группы после блокировки, поэтому видит и эту правку.

    Ограничение слияния: `trip_id` остаётся от первой правки. Пересчёт всё равно проверяет все
    активные трансферы группы, от `trip_id` зависят только tie-break при исключении и `trip_id` в
    записях истории.

    DO NOTHING, а не DO UPDATE: воркер держит строку задания `FOR UPDATE` и ждёт `lock_event`,
    который держит производитель, и UPDATE этой строки дал бы взаимную блокировку. DO NOTHING на
    блокировку строки не ждёт. Задание в статусе `failed` новому не мешает.
    """
    await session.execute(
        insert(BackgroundJob)
        .values(event_id=event_id, kind=JobKind.RECONCILE_TRIP, trip_id=trip_id)
        .on_conflict_do_nothing(
            index_elements=[BackgroundJob.event_id, BackgroundJob.kind],
            # литерал, а не параметр: по параметру Postgres не подберёт частичный индекс
            index_where=text(f"status = '{JobStatus.PENDING.value}'"),
        )
    )


def _observe_attempt(kind: str, outcome: JobOutcome, started: float) -> None:
    """Учитывает завершённую попытку в метриках; вызывать только после commit."""
    metrics.worker_jobs_total.labels(kind, outcome.value).inc()
    metrics.worker_job_duration_seconds.labels(kind).observe(time.perf_counter() - started)


async def _register_failure(
    session: AsyncSession, job: BackgroundJob, exc: Exception, started: float
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
        # clock_timestamp(), а не now(): now() — начало транзакции, то есть момент забора задания,
        # и долгий пересчёт, упавший позже задержки, получил бы повтор сразу, без паузы
        values["run_after"] = func.clock_timestamp(type_=DateTime(timezone=True)) + retry_delay(
            job.attempts
        )
    kind, job_id = job.kind.value, job.id
    await session.execute(update(BackgroundJob).where(BackgroundJob.id == job_id).values(**values))
    await session.commit()
    _observe_attempt(kind, outcome, started)
    # текст исключения может содержать персональные данные (значения строк в ошибках БД), поэтому
    # в сообщение он не попадает, а форматтер JSON-логов пишет в traceback только кадры стека
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
        started = time.perf_counter()
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
                return await _register_failure(session, job, exc, started)
            await session.execute(delete(BackgroundJob).where(BackgroundJob.id == job_id))
            await session.commit()
            _observe_attempt(kind, JobOutcome.DONE, started)
            logger.info(
                "Пересчёт трансферов выполнен: исключено заявок %d, расформировано трансферов %d,"
                " размещено заявок %d",
                result.excluded,
                result.dissolved,
                result.placed,
                extra={"kind": kind, "outcome": JobOutcome.DONE.value},
            )
            return JobOutcome.DONE
        finally:
            context.job_id.reset(token)


async def observe_queue(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Обновляет gauge'и очереди одним запросом: число заданий по статусам и возраст старейшего.

    Возраст считается от `created_at` самого старого задания в статусе `pending` (0, если
    очередь пуста); статусы без заданий получают 0, чтобы ряд не пропадал.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    BackgroundJob.status,
                    func.count(),
                    extract("epoch", func.now() - func.min(BackgroundJob.created_at)),
                ).group_by(BackgroundJob.status)
            )
        ).all()
    depth = {status: 0 for status in JobStatus}
    oldest = 0.0
    for status, count, age in rows:
        depth[status] = count
        if status == JobStatus.PENDING:
            oldest = max(float(age), 0.0)
    for status, count in depth.items():
        metrics.worker_queue_depth.labels(status.value).set(count)
    metrics.worker_oldest_job_age_seconds.set(oldest)
