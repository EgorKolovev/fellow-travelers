"""Таблица background_jobs: значения по умолчанию, CHECK на статус и вид, trip_id без FK."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackgroundJob, JobKind, JobStatus
from tests.factories import make_event, make_job


async def _fresh(session: AsyncSession, job_id: uuid.UUID) -> BackgroundJob:
    stmt = (
        select(BackgroundJob)
        .where(BackgroundJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def test_defaults(db_session: AsyncSession) -> None:
    event = await make_event(db_session)

    job = await _fresh(db_session, (await make_job(db_session, event_id=event.id)).id)

    assert job.kind == JobKind.RECONCILE_TRIP
    assert job.status == JobStatus.PENDING
    assert job.attempts == 0
    assert job.trip_id is None
    assert job.last_error is None
    now = await db_session.scalar(text("SELECT now()"))
    assert job.run_after == job.created_at == now


async def test_trip_id_has_no_foreign_key(db_session: AsyncSession) -> None:
    # задание не мешает удалить рейс и переживает его
    job = await make_job(db_session, trip_id=uuid.uuid4())

    assert (await _fresh(db_session, job.id)).trip_id == job.trip_id


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("status", "running", "ck_background_jobs_job_status"),
        ("kind", "poll_trips", "ck_background_jobs_job_kind"),
    ],
)
async def test_unknown_status_and_kind_are_rejected(
    db_session: AsyncSession, column: str, value: str, constraint: str
) -> None:
    job = await make_job(db_session)

    with pytest.raises(IntegrityError, match=constraint):
        await db_session.execute(
            text(f"UPDATE background_jobs SET {column} = :value WHERE id = :id"),
            {"value": value, "id": job.id},
        )


async def test_pending_index_is_partial(db_session: AsyncSession) -> None:
    definition = await db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE indexname = 'ix_background_jobs_pending_run_after'"
        )
    )

    assert definition is not None
    assert "WHERE" in definition
    assert "'pending'" in definition
