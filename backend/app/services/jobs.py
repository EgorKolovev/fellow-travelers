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
# пауза в памяти воркера, если не удалось даже записать ошибку задания: `retry_delay(0)`
_PAUSE_AFTER_DOUBLE_FAILURE_SECONDS = _RETRY_BASE_SECONDS

# job_id -> момент (time.monotonic()), до которого воркер не берёт задание: память процесса,
# пережить рестарт не должна — после рестарта задание просто берётся снова
_paused_until: dict[uuid.UUID, float] = {}


def _paused(job_id: uuid.UUID) -> bool:
    """На паузе ли задание после двойного сбоя — упали и обработка, и запись ошибки."""
    deadline = _paused_until.get(job_id)
    if deadline is None:
        return False
    if time.monotonic() < deadline:
        return True
    del _paused_until[job_id]
    return False


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
    с ним. После вставки `trip_id` задания обновляется на самую свежую правку: записи истории
    исключений (`transfer_left.trip_id`) и tie-break при исключении указывают на правку-причину.
    Пересчёт в любом случае читает состояние группы после блокировки, поэтому видит обе правки.

    Вставка — DO NOTHING, а не DO UPDATE: воркер держит строку задания `FOR UPDATE` и ждёт
    `lock_event`, который держит производитель, и UPDATE этой строки дал бы взаимную блокировку.
    DO NOTHING на блокировку строки не ждёт, поэтому и обновление `trip_id` идёт отдельным
    UPDATE с `FOR UPDATE SKIP LOCKED` в подзапросе: строку, взятую воркером, пропускаем — такой
    воркер вот-вот пересчитает группу со старым `trip_id` (и правку, и состав он видит верные,
    устаревает только атрибуция истории). Задание в статусе `failed` новому не мешает.
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
    await session.execute(
        update(BackgroundJob)
        .values(trip_id=trip_id)
        .where(
            BackgroundJob.id.in_(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.event_id == event_id,
                    BackgroundJob.kind == JobKind.RECONCILE_TRIP,
                    BackgroundJob.status == JobStatus.PENDING,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .execution_options(synchronize_session=False)
    )


def _observe_attempt(kind: str, outcome: JobOutcome, started: float) -> None:
    """Учитывает завершённую попытку в метриках; вызывать только после commit."""
    metrics.worker_jobs_total.labels(kind, outcome.value).inc()
    metrics.worker_job_duration_seconds.labels(kind).observe(time.perf_counter() - started)


async def _register_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    kind: str,
    attempts: int,
    exc: Exception,
    started: float,
) -> JobOutcome | None:
    """Учитывает неудачную попытку: счётчик, пауза до повтора или `failed`; лог и commit.

    Отдельная короткая транзакция на новой сессии: сессия с упавшим пересчётом уже закрыта, и её
    возможная порча (например, разрыв соединения) учёту не мешает. Если не удалось и это, счётчик
    и пауза в БД не записаны — остаётся только пауза в памяти воркера (`_paused`), чтобы он не
    забирал то же задание снова на каждом опросе без экспоненциальной задержки; `None` просит
    цикл пропустить тик.
    """
    attempts += 1
    values: dict[str, object] = {
        "attempts": attempts,
        "last_error": type(exc).__name__[:_LAST_ERROR_LENGTH],
    }
    outcome = JobOutcome.FAILED if attempts >= MAX_ATTEMPTS else JobOutcome.RETRY
    if outcome == JobOutcome.FAILED:
        values["status"] = JobStatus.FAILED
    else:
        # clock_timestamp(), а не now(): пауза считается от момента записи ошибки, а не от
        # начала транзакции регистрации, которая стартует позже самой ошибки
        values["run_after"] = func.clock_timestamp(type_=DateTime(timezone=True)) + retry_delay(
            attempts - 1
        )
    token = context.job_id.set(job_id)
    try:
        try:
            async with session_factory() as session:
                await session.execute(
                    update(BackgroundJob).where(BackgroundJob.id == job_id).values(**values)
                )
                await session.commit()
        except Exception as register_exc:
            # двойной сбой: упал и пересчёт, и запись ошибки. В БД ничего не изменилось, поэтому
            # пауза до повтора держится в памяти процесса (пережить рестарт воркера она не должна)
            _paused_until[job_id] = time.monotonic() + _PAUSE_AFTER_DOUBLE_FAILURE_SECONDS
            # в логе — traceback исходной ошибки пересчёта, класс ошибки записи в сообщении:
            # имена классов безопасны, тексты могут содержать персональные данные
            logger.error(
                "Не удалось записать ошибку задания (%s): пауза до повтора без записи в БД",
                type(register_exc).__name__,
                extra={"kind": kind, "attempts": attempts},
                exc_info=exc,
            )
            return None
        _observe_attempt(kind, outcome, started)
        # текст исключения может содержать персональные данные (значения строк в ошибках БД),
        # поэтому в сообщение он не попадает, а форматтер JSON-логов пишет в traceback только
        # кадры стека
        extra = {"kind": kind, "outcome": outcome.value, "attempts": attempts}
        if outcome == JobOutcome.FAILED:
            logger.error("Задание не выполнено, повторов больше нет", extra=extra, exc_info=exc)
        else:
            logger.warning("Задание не выполнено, будет повтор", extra=extra, exc_info=exc)
        return outcome
    finally:
        context.job_id.reset(token)


async def process_next(session_factory: async_sessionmaker[AsyncSession]) -> JobOutcome | None:
    """Забирает одно готовое задание и выполняет его в одной транзакции.

    Возвращает `None`, если готовых заданий нет либо первое стоит на внутрипроцессной паузе
    после двойного сбоя (задание не берётся, но цикл воркера делает обычную паузу между
    опросами). Задание блокируется `FOR UPDATE SKIP LOCKED`, поэтому несколько воркеров делят
    очередь, не ожидая друг друга. Пересчёт идёт внутри savepoint, под `lock_event` группы.
    Успех удаляет задание в той же транзакции: повторная доставка ничего не меняет. Ошибка
    откатывает только пересчёт: `attempts` растёт, `run_after` сдвигается на `retry_delay`,
    в `last_error` пишется имя класса исключения (текст может содержать персональные данные).
    После `MAX_ATTEMPTS` попыток задание получает `failed` и больше не берётся.

    Учитывает ошибку `_register_failure` на отдельной короткой транзакции: сессия с пересчётом
    к этому моменту закрыта (строка задания отпущена), и сбой пересчёта не мешает записать
    попытку. Если не удалась и запись — задание остаётся нетронутым, воркер держит его id
    в памяти и пропускает, пока действует `_paused`.
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
        if job is None or _paused(job.id):
            return None
        started = time.perf_counter()
        job_id, kind, attempts = job.id, job.kind.value, job.attempts
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
                exc_to_register = exc
            else:
                await session.execute(delete(BackgroundJob).where(BackgroundJob.id == job_id))
                await session.commit()
                _observe_attempt(kind, JobOutcome.DONE, started)
                logger.info(
                    "Пересчёт трансферов выполнен: исключено заявок %d, расформировано трансферов"
                    " %d, размещено заявок %d",
                    result.excluded,
                    result.dissolved,
                    result.placed,
                    extra={"kind": kind, "outcome": JobOutcome.DONE.value},
                )
                return JobOutcome.DONE
        finally:
            context.job_id.reset(token)
    # сессия с пересчётом закрыта: строка задания отпущена, регистрация не заблокируется на ней
    return await _register_failure(
        session_factory,
        job_id=job_id,
        kind=kind,
        attempts=attempts,
        exc=exc_to_register,
        started=started,
    )


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
