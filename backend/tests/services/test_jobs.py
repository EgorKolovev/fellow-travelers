"""Очередь заданий: постановка, повторы с паузой, предел попыток, откат пересчёта при ошибке.

`process_next` работает в своей сессии на общем соединении теста (`shared_session_factory`),
поэтому данные `db_session` ему видны, а в конце всё откатывается. Гонки двух соединений
проверяются отдельно, в `test_jobs_concurrency.py`.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    ApplicationStatus,
    BackgroundJob,
    JobKind,
    JobStatus,
    Transfer,
    Trip,
)
from app.observability import context
from app.services import jobs as jobs_service
from app.services import matching as matching_service
from app.services.jobs import (
    MAX_ATTEMPTS,
    JobOutcome,
    enqueue_reconcile,
    process_next,
    retry_delay,
)
from app.services.matching import ReconcileResult, match_application
from tests.factories import make_application, make_event, make_job, make_trip
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.jobs import jobs_of, shared_session_factory

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
SECRET = "+79991234567 Ivan Ivanov"


class BoomError(Exception):
    pass


# ---------------------------------------------------------------- пауза и постановка


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(0, 5), (1, 10), (2, 20), (3, 40), (4, 80), (5, 160), (6, 320), (7, 640), (8, 900), (40, 900)],
)
def test_retry_delay_doubles_and_is_capped(attempts: int, seconds: int) -> None:
    assert retry_delay(attempts) == timedelta(seconds=seconds)


async def test_enqueue_adds_pending_job_in_the_callers_transaction(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    trip_id = uuid.uuid4()

    savepoint = await db_session.begin_nested()
    await enqueue_reconcile(db_session, event_id=event.id, trip_id=trip_id)
    (job,) = await jobs_of(db_session, event.id)
    assert (job.status, job.kind, job.trip_id) == (
        JobStatus.PENDING,
        JobKind.RECONCILE_TRIP,
        trip_id,
    )

    await savepoint.rollback()

    # откат транзакции вызывающего уносит и задание
    assert await jobs_of(db_session, event.id) == []


async def test_second_enqueue_merges_into_the_pending_job(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first_trip, second_trip = uuid.uuid4(), uuid.uuid4()

    await enqueue_reconcile(db_session, event_id=event.id, trip_id=first_trip)
    await enqueue_reconcile(db_session, event_id=event.id, trip_id=second_trip)

    (job,) = await jobs_of(db_session, event.id)
    assert job.trip_id == first_trip  # при слиянии trip_id остаётся от первой правки


async def test_enqueue_in_other_group_is_not_merged(db_session: AsyncSession) -> None:
    first, second = await make_event(db_session), await make_event(db_session)

    await enqueue_reconcile(db_session, event_id=first.id, trip_id=uuid.uuid4())
    await enqueue_reconcile(db_session, event_id=second.id, trip_id=uuid.uuid4())

    assert len(await jobs_of(db_session, first.id)) == 1
    assert len(await jobs_of(db_session, second.id)) == 1


async def test_failed_job_does_not_block_a_new_pending_one(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED, attempts=MAX_ATTEMPTS)

    await enqueue_reconcile(db_session, event_id=event.id, trip_id=uuid.uuid4())
    await enqueue_reconcile(db_session, event_id=event.id, trip_id=uuid.uuid4())

    jobs = await jobs_of(db_session, event.id)
    assert sorted(job.status for job in jobs) == [JobStatus.FAILED, JobStatus.PENDING]


async def test_new_pending_job_is_possible_once_the_old_one_is_done(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await enqueue_reconcile(db_session, event_id=event.id, trip_id=uuid.uuid4())
    assert await process_next(shared_session_factory(db_session)) == JobOutcome.DONE

    await enqueue_reconcile(db_session, event_id=event.id, trip_id=uuid.uuid4())

    assert len(await jobs_of(db_session, event.id)) == 1


async def test_database_forbids_two_pending_jobs_of_one_kind(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await make_job(db_session, event_id=event.id)

    with pytest.raises(IntegrityError, match="uq_background_jobs_event_kind_pending"):
        async with db_session.begin_nested():
            await make_job(db_session, event_id=event.id)

    # а несколько failed подряд индекс допускает
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED)
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED)


# ---------------------------------------------------------------- успех


async def _pair(session: AsyncSession) -> tuple[uuid.UUID, Application, Application, Trip]:
    """Пара в одном трансфере; второй из пары прилетает на своём рейсе."""
    event = await make_event(session)
    first_trip = await make_trip(session, event_id=event.id, scheduled_arrival=BASE)
    second_trip = await make_trip(session, event_id=event.id, scheduled_arrival=BASE)
    first = await make_application(
        session, event_id=event.id, trip_id=first_trip.id, baggage_count=0
    )
    second = await make_application(
        session, event_id=event.id, trip_id=second_trip.id, baggage_count=0
    )
    assert await match_application(session, second) is not None
    return event.id, first, second, second_trip


async def test_process_next_on_empty_queue_returns_none(db_session: AsyncSession) -> None:
    assert await process_next(shared_session_factory(db_session)) is None


async def test_success_applies_reconcile_and_deletes_job(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    event_id, first, second, trip = await _pair(db_session)
    trip.estimated_arrival = BASE + timedelta(minutes=30)  # ожидание 15 минут
    await db_session.flush()
    job = await make_job(db_session, event_id=event_id, trip_id=trip.id)
    job_id = job.id

    outcome = await process_next(shared_session_factory(db_session))

    assert outcome == JobOutcome.DONE
    assert await jobs_of(db_session, event_id) == []
    for member in (first, second):
        await db_session.refresh(member)
        assert (member.status, member.transfer_id) == (ApplicationStatus.SEARCHING, None)
    assert context.job_id.get() is None
    rows = await audit_rows(db_session, event_id)
    assert [(row.action, row.job_id) for row in rows] == [
        ("transfer_created", None),
        ("transfer_left", job_id),
        ("transfer_left", job_id),
        ("transfer_dissolved", job_id),
    ]
    assert rows[1].reason == "incompatible_after_change"
    assert rows[1].trip_id == trip.id
    await assert_history_matches_transfers(db_session, event_id)
    (summary,) = [r for r in caplog.records if r.name == jobs_service.__name__]
    assert summary.getMessage() == (
        "Пересчёт трансферов выполнен: исключено заявок 1, расформировано трансферов 1,"
        " размещено заявок 0"
    )


async def test_success_without_changes_deletes_job_and_writes_nothing(
    db_session: AsyncSession,
) -> None:
    event_id, first, _, trip = await _pair(db_session)
    trip.estimated_arrival = BASE + timedelta(minutes=10)  # в пределах ожидания
    await db_session.flush()
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    before = len(await audit_rows(db_session, event_id))

    outcome = await process_next(shared_session_factory(db_session))

    assert outcome == JobOutcome.DONE
    assert await jobs_of(db_session, event_id) == []
    assert len(await audit_rows(db_session, event_id)) == before
    await db_session.refresh(first)
    assert first.status == ApplicationStatus.ASSIGNED


async def test_second_job_for_same_trip_changes_nothing(db_session: AsyncSession) -> None:
    event_id, _, _, trip = await _pair(db_session)
    trip.estimated_arrival = BASE + timedelta(minutes=30)
    await db_session.flush()
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    factory = shared_session_factory(db_session)

    assert await process_next(factory) == JobOutcome.DONE
    rows_before = len(await audit_rows(db_session, event_id))
    # то же задание приходит снова (повторная доставка): первое уже выполнено и удалено
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    assert await process_next(factory) == JobOutcome.DONE

    assert len(await audit_rows(db_session, event_id)) == rows_before
    assert await process_next(factory) is None


async def test_jobs_are_taken_oldest_first(db_session: AsyncSession) -> None:
    later, earlier = await make_event(db_session), await make_event(db_session)
    now = await db_session.scalar(select(func.now()))
    assert now is not None
    late = await make_job(db_session, event_id=later.id, run_after=now - timedelta(minutes=1))
    await make_job(db_session, event_id=earlier.id, run_after=now - timedelta(hours=1))
    factory = shared_session_factory(db_session)

    assert await process_next(factory) == JobOutcome.DONE
    assert await jobs_of(db_session, earlier.id) == []
    assert [job.id for job in await jobs_of(db_session, later.id)] == [late.id]
    assert await process_next(factory) == JobOutcome.DONE
    assert await jobs_of(db_session, later.id) == []


# ---------------------------------------------------------------- ошибка


@pytest.fixture
def failing_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: Any, **kwargs: Any) -> ReconcileResult:
        raise BoomError(SECRET)

    monkeypatch.setattr(jobs_service, "reconcile_event", fail)


async def _delay_until_run(session: AsyncSession, job_id: uuid.UUID) -> timedelta:
    """На сколько `run_after` задания позже начала транзакции теста (`now()`)."""
    value: object = await session.scalar(
        select(BackgroundJob.run_after - func.now()).where(BackgroundJob.id == job_id)
    )
    assert isinstance(value, timedelta)
    return value


async def _fresh_job(session: AsyncSession, job_id: uuid.UUID) -> BackgroundJob:
    stmt = (
        select(BackgroundJob)
        .where(BackgroundJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def test_failure_schedules_retry_and_keeps_only_class_name(
    db_session: AsyncSession, failing_reconcile: None, caplog: pytest.LogCaptureFixture
) -> None:
    job = await make_job(db_session)
    job_id = job.id
    caplog.set_level(logging.WARNING)

    outcome = await process_next(shared_session_factory(db_session))

    assert outcome == JobOutcome.RETRY
    stored = await _fresh_job(db_session, job_id)
    assert (stored.status, stored.attempts, stored.last_error) == (
        JobStatus.PENDING,
        1,
        "BoomError",
    )
    # пауза считается от момента ошибки (clock_timestamp), а не от начала транзакции (now)
    assert (
        timedelta(seconds=5) <= await _delay_until_run(db_session, job_id) < timedelta(seconds=10)
    )
    (record,) = [r for r in caplog.records if r.name == jobs_service.__name__]
    assert record.levelno == logging.WARNING
    assert (record.__dict__["kind"], record.__dict__["attempts"]) == ("reconcile_trip", 1)
    assert record.__dict__["outcome"] == "retry"
    assert SECRET not in record.getMessage()
    assert context.job_id.get() is None


async def test_failed_attempt_is_not_taken_before_run_after(
    db_session: AsyncSession, failing_reconcile: None
) -> None:
    await make_job(db_session)
    factory = shared_session_factory(db_session)

    assert await process_next(factory) == JobOutcome.RETRY

    assert await process_next(factory) is None  # пауза ещё не прошла


async def test_failure_rolls_back_reconcile_changes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, first, second, trip = await _pair(db_session)
    trip.estimated_arrival = BASE + timedelta(minutes=30)
    await db_session.flush()
    job = await make_job(db_session, event_id=event_id, trip_id=trip.id)
    original = matching_service.reconcile_event
    rows_before = len(await audit_rows(db_session, event_id))

    async def apply_then_fail(*args: Any, **kwargs: Any) -> ReconcileResult:
        result = await original(*args, **kwargs)
        assert result.excluded == 1  # пересчёт действительно успел изменить состав
        raise BoomError(SECRET)

    monkeypatch.setattr(jobs_service, "reconcile_event", apply_then_fail)

    assert await process_next(shared_session_factory(db_session)) == JobOutcome.RETRY

    for member in (first, second):
        await db_session.refresh(member)
        assert member.status == ApplicationStatus.ASSIGNED
    assert len(await audit_rows(db_session, event_id)) == rows_before
    assert await db_session.scalar(select(func.count()).select_from(Transfer)) == 1
    assert (await _fresh_job(db_session, job.id)).attempts == 1


async def test_eighth_failure_marks_job_failed_and_it_is_not_taken_again(
    db_session: AsyncSession, failing_reconcile: None, caplog: pytest.LogCaptureFixture
) -> None:
    job = await make_job(db_session)
    job_id = job.id
    factory = shared_session_factory(db_session)
    caplog.set_level(logging.WARNING)

    outcomes = []
    for _ in range(MAX_ATTEMPTS):
        outcomes.append(await process_next(factory))
        # пауза до повтора кончилась: следующую попытку можно брать сразу
        await db_session.execute(
            update(BackgroundJob).where(BackgroundJob.id == job_id).values(run_after=func.now())
        )

    assert outcomes == [JobOutcome.RETRY] * (MAX_ATTEMPTS - 1) + [JobOutcome.FAILED]
    stored = await _fresh_job(db_session, job_id)
    assert (stored.status, stored.attempts, stored.last_error) == (
        JobStatus.FAILED,
        MAX_ATTEMPTS,
        "BoomError",
    )
    assert await process_next(factory) is None
    levels = [r.levelno for r in caplog.records if r.name == jobs_service.__name__]
    assert levels == [logging.WARNING] * (MAX_ATTEMPTS - 1) + [logging.ERROR]
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_backoff_grows_with_attempts(
    db_session: AsyncSession, failing_reconcile: None
) -> None:
    job = await make_job(db_session)
    job_id = job.id
    factory = shared_session_factory(db_session)

    delays = []
    for _ in range(4):
        await process_next(factory)
        delays.append(await _delay_until_run(db_session, job_id))
        await db_session.execute(
            update(BackgroundJob).where(BackgroundJob.id == job_id).values(run_after=func.now())
        )

    for delay, seconds in zip(delays, (5, 10, 20, 40), strict=True):
        assert timedelta(seconds=seconds) <= delay < timedelta(seconds=seconds + 5)


async def test_failed_and_future_jobs_are_skipped(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    now = await db_session.scalar(select(func.now()))
    assert now is not None
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED, attempts=MAX_ATTEMPTS)
    await make_job(db_session, event_id=event.id, run_after=now + timedelta(seconds=30))

    assert await process_next(shared_session_factory(db_session)) is None

    assert len(await jobs_of(db_session, event.id)) == 2


async def test_job_without_trip_id_runs_reconcile_without_changed_trips(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await make_job(db_session, event_id=event.id, trip_id=None)

    assert await process_next(shared_session_factory(db_session)) == JobOutcome.DONE

    assert await jobs_of(db_session, event.id) == []


async def test_retry_pause_counts_from_the_error_not_from_job_pick_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`now()` стоит на начале транзакции: долгий пересчёт съел бы паузу, повтор пошёл бы сразу."""
    job = await make_job(db_session)
    job_id = job.id
    failed_at: list[datetime] = []

    async def slow_failure(session: AsyncSession, *args: Any, **kwargs: Any) -> ReconcileResult:
        await session.execute(text("SELECT pg_sleep(0.3)"))
        moment = await session.scalar(select(func.clock_timestamp()))
        assert moment is not None
        failed_at.append(moment)
        raise BoomError(SECRET)

    monkeypatch.setattr(jobs_service, "reconcile_event", slow_failure)

    assert await process_next(shared_session_factory(db_session)) == JobOutcome.RETRY

    run_after = (await _fresh_job(db_session, job_id)).run_after
    assert run_after >= failed_at[0] + retry_delay(0)
    assert run_after < failed_at[0] + retry_delay(0) + timedelta(seconds=5)
