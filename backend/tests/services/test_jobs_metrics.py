"""Метрики воркера: исходы заданий, длительность, снимок очереди; откат пересчёта их не двигает."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    AuditReason,
    BackgroundJob,
    JobKind,
    JobStatus,
    Trip,
)
from app.observability.metrics import MatchTrigger, PlacementKind
from app.services import jobs as jobs_service
from app.services import matching as matching_service
from app.services.jobs import MAX_ATTEMPTS, JobOutcome, observe_queue, process_next
from app.services.matching import ReconcileResult, match_application
from tests.factories import make_application, make_event, make_job, make_trip
from tests.helpers.jobs import shared_session_factory
from tests.helpers.metrics import sample, total

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
KIND = JobKind.RECONCILE_TRIP.value


class BoomError(Exception):
    pass


def outcomes(outcome: JobOutcome) -> float:
    return sample("ft_worker_jobs_total", kind=KIND, outcome=outcome.value)


def durations() -> float:
    return sample("ft_worker_job_duration_seconds_count", kind=KIND)


def exclusions() -> float:
    return sample(
        "ft_transfer_exclusions_total", reason=AuditReason.INCOMPATIBLE_AFTER_CHANGE.value
    )


async def _delayed_pair(session: AsyncSession) -> tuple[uuid.UUID, Trip]:
    """Пара в трансфере; рейс второй заявки опаздывает на 30 минут при ожидании 15."""
    event = await make_event(session)
    trips = [await make_trip(session, event_id=event.id, scheduled_arrival=BASE) for _ in range(2)]
    applications: list[Application] = [
        await make_application(session, event_id=event.id, trip_id=trip.id, baggage_count=0)
        for trip in trips
    ]
    assert await match_application(session, applications[1]) is not None
    trips[1].estimated_arrival = BASE + timedelta(minutes=30)
    await session.flush()
    return event.id, trips[1]


# ---------------------------------------------------------------- исходы заданий


async def test_done_is_counted_once_with_duration(db_session: AsyncSession) -> None:
    event_id, trip = await _delayed_pair(db_session)
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    done, timed, excluded = outcomes(JobOutcome.DONE), durations(), exclusions()

    assert await process_next(shared_session_factory(db_session)) == JobOutcome.DONE

    assert outcomes(JobOutcome.DONE) == done + 1
    assert durations() == timed + 1
    assert exclusions() == excluded + 1  # метрики пересчёта выросли после commit задания


async def test_empty_queue_counts_nothing(db_session: AsyncSession) -> None:
    before = (total("ft_worker_jobs_total"), durations())

    assert await process_next(shared_session_factory(db_session)) is None

    assert (total("ft_worker_jobs_total"), durations()) == before


async def test_retry_and_failed_are_counted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> ReconcileResult:
        raise BoomError

    monkeypatch.setattr(jobs_service, "reconcile_event", fail)
    job = await make_job(db_session)
    factory = shared_session_factory(db_session)
    retries, failures, timed = outcomes(JobOutcome.RETRY), outcomes(JobOutcome.FAILED), durations()

    for _ in range(MAX_ATTEMPTS):
        await process_next(factory)
        await db_session.execute(
            update(BackgroundJob).where(BackgroundJob.id == job.id).values(run_after=func.now())
        )

    assert outcomes(JobOutcome.RETRY) == retries + MAX_ATTEMPTS - 1
    assert outcomes(JobOutcome.FAILED) == failures + 1
    assert durations() == timed + MAX_ATTEMPTS


async def test_failed_attempt_rolls_back_subscenario_counters(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, trip = await _delayed_pair(db_session)
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    original = matching_service.reconcile_event

    async def apply_then_fail(*args: Any, **kwargs: Any) -> ReconcileResult:
        result = await original(*args, **kwargs)
        assert result.excluded == 1  # пересчёт успел изменить состав и учесть метрики
        raise BoomError

    monkeypatch.setattr(jobs_service, "reconcile_event", apply_then_fail)
    snapshot = {
        "exclusions": total("ft_transfer_exclusions_total"),
        "dissolved": total("ft_transfers_dissolved_total"),
        "placements": total("ft_match_placements_total"),
        "match_time": total("ft_match_duration_seconds_count"),
    }
    retries = outcomes(JobOutcome.RETRY)

    assert await process_next(shared_session_factory(db_session)) == JobOutcome.RETRY

    assert outcomes(JobOutcome.RETRY) == retries + 1
    assert {
        "exclusions": total("ft_transfer_exclusions_total"),
        "dissolved": total("ft_transfers_dissolved_total"),
        "placements": total("ft_match_placements_total"),
        "match_time": total("ft_match_duration_seconds_count"),
    } == snapshot


async def test_reconcile_fill_counts_no_refusals(db_session: AsyncSession) -> None:
    event_id, trip = await _delayed_pair(db_session)
    await make_job(db_session, event_id=event_id, trip_id=trip.id)
    before = sum(
        sample("ft_match_placements_total", trigger=MatchTrigger.RECONCILE.value, kind=kind.value)
        for kind in PlacementKind
    )

    await process_next(shared_session_factory(db_session))

    # обе заявки пары вернулись в поиск и не нашли места: отказ при пересчёте не размещение
    after = sum(
        sample("ft_match_placements_total", trigger=MatchTrigger.RECONCILE.value, kind=kind.value)
        for kind in PlacementKind
    )
    assert after == before


# ---------------------------------------------------------------- снимок очереди


async def test_observe_queue_sets_depth_and_oldest_age(db_session: AsyncSession) -> None:
    event, other = await make_event(db_session), await make_event(db_session)
    now = await db_session.scalar(select(func.now()))
    assert now is not None
    await make_job(db_session, event_id=event.id, created_at=now - timedelta(seconds=90))
    await make_job(db_session, event_id=other.id, created_at=now - timedelta(seconds=10))
    await make_job(db_session, event_id=event.id, status=JobStatus.FAILED, attempts=8)
    # у failed возраст не учитывается, даже если оно старше всех pending
    await make_job(
        db_session,
        event_id=event.id,
        status=JobStatus.FAILED,
        created_at=now - timedelta(hours=5),
    )

    await observe_queue(shared_session_factory(db_session))

    assert sample("ft_worker_queue_depth", status="pending") == 2
    assert sample("ft_worker_queue_depth", status="failed") == 2
    assert sample("ft_worker_oldest_job_age_seconds") == 90


async def test_observe_queue_on_empty_queue_resets_gauges(db_session: AsyncSession) -> None:
    await observe_queue(shared_session_factory(db_session))
    event = await make_event(db_session)
    await make_job(db_session, event_id=event.id)
    await observe_queue(shared_session_factory(db_session))
    assert sample("ft_worker_queue_depth", status="pending") == 1

    await db_session.execute(delete(BackgroundJob))
    await observe_queue(shared_session_factory(db_session))

    assert sample("ft_worker_queue_depth", status="pending") == 0
    assert sample("ft_worker_queue_depth", status="failed") == 0
    assert sample("ft_worker_oldest_job_age_seconds") == 0
