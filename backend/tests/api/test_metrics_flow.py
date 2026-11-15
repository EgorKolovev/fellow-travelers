"""Метрики подбора и воркера на сценариях через HTTP: триггеры, откат и закрытые лейблы."""

import re
import uuid

import pytest
from httpx import AsyncClient
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditReason, Event, JobKind, JobStatus, User
from app.observability.metrics import MatchTrigger, PlacementKind
from app.services.jobs import JobOutcome, observe_queue, process_next
from tests.api.test_applications import act, apply, body, get_app, join, put_app, trip_at
from tests.api.test_trips import patch_trip
from tests.factories import make_event, make_membership, make_trip, make_user
from tests.helpers.jobs import shared_session_factory
from tests.helpers.metrics import label_values, sample, total

UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")
ADMIN_TELEGRAM_ID = 500
MISSING_TRIP_ID = uuid.UUID("6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f")

PLACEMENTS = "ft_match_placements_total"
MATCH_METRICS = (
    PLACEMENTS,
    "ft_match_duration_seconds_count",
    "ft_transfer_exclusions_total",
    "ft_transfers_dissolved_total",
)


def placed(trigger: MatchTrigger, kind: PlacementKind) -> float:
    return sample(PLACEMENTS, trigger=trigger.value, kind=kind.value)


def placements_except(excluded: MatchTrigger) -> float:
    return sum(
        placed(trigger, kind)
        for trigger in MatchTrigger
        if trigger != excluded
        for kind in PlacementKind
    )


@pytest.fixture
async def event(db_session: AsyncSession, user: User) -> Event:
    created = await make_event(db_session)
    await make_membership(db_session, event_id=created.id, user_id=user.id)
    return created


@pytest.fixture
async def p2(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 101)


@pytest.fixture
async def p3(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, 102)


# ---------------------------------------------------------------- триггеры


async def test_put_counts_waiting_pair_and_join_with_application_trigger(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User, p3: User
) -> None:
    waiting = placed(MatchTrigger.APPLICATION, PlacementKind.NONE)
    pairs = placed(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR)
    joins = placed(MatchTrigger.APPLICATION, PlacementKind.JOIN)

    await apply(api_client, db_session, event, 42)
    assert placed(MatchTrigger.APPLICATION, PlacementKind.NONE) == waiting + 1
    await apply(api_client, db_session, event, 101)
    assert placed(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR) == pairs + 1
    await apply(api_client, db_session, event, 102)
    assert placed(MatchTrigger.APPLICATION, PlacementKind.JOIN) == joins + 1


async def test_resume_search_counts_with_application_trigger(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 101)
    await apply(api_client, db_session, event, 42, seek_companions=False)
    pairs = placed(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR)

    await act(api_client, db_session, event, "resume-search")

    assert placed(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR) == pairs + 1


@pytest.mark.parametrize(("action", "reason"), [("go-solo", "go_solo"), ("cancel", "cancel")])
async def test_leaving_counts_exclusions_dissolution_and_leave_placements(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    p2: User,
    p3: User,
    action: str,
    reason: str,
) -> None:
    # 102 прилетает на 20 минут позже 42 и к паре 42 + 101 не садится, но с 101 совместима
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101, minutes=10)
    await apply(api_client, db_session, event, 102, minutes=20)
    pairs = placed(MatchTrigger.LEAVE, PlacementKind.NEW_PAIR)
    other_triggers = placements_except(MatchTrigger.LEAVE)
    left = sample("ft_transfer_exclusions_total", reason=reason)
    remaining = sample("ft_transfer_exclusions_total", reason="member_left")
    gone = sample("ft_transfers_dissolved_total", reason=reason)

    await act(api_client, db_session, event, action)

    assert placed(MatchTrigger.LEAVE, PlacementKind.NEW_PAIR) == pairs + 1
    assert sample("ft_transfer_exclusions_total", reason=reason) == left + 1
    assert sample("ft_transfer_exclusions_total", reason="member_left") == remaining + 1
    assert sample("ft_transfers_dissolved_total", reason=reason) == gone + 1
    # выход не запускает размещения с другими триггерами
    assert placements_except(MatchTrigger.LEAVE) == other_triggers


# ---------------------------------------------------------------- откат и отказ


async def test_rejected_requests_do_not_count(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, p2: User
) -> None:
    await apply(api_client, db_session, event, 42)
    await apply(api_client, db_session, event, 101)
    trip_id = await trip_at(db_session, event, 1)
    before = {name: total(name) for name in MATCH_METRICS}

    locked = await put_app(api_client, db_session, event, body(trip_id))  # 409: уже в трансфере
    unknown = await put_app(api_client, db_session, event, body(MISSING_TRIP_ID))  # 422

    assert (locked.status_code, unknown.status_code) == (409, 422)
    assert {name: total(name) for name in MATCH_METRICS} == before


async def test_trip_patch_error_counts_no_job_metrics(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    admin_event = await make_event(db_session, admin_id=user.id)
    trip = await make_trip(db_session, event_id=admin_event.id)
    before = (total("ft_worker_jobs_total"), total("ft_worker_job_duration_seconds_count"))

    response = await patch_trip(api_client, trip.id, {"scheduled_arrival": "2026-11-12T01:00:00"})

    assert response.status_code == 422
    assert await process_next(shared_session_factory(db_session)) is None
    assert (total("ft_worker_jobs_total"), total("ft_worker_job_duration_seconds_count")) == before


# ---------------------------------------------------------------- лейблы


async def test_labels_are_closed_sets_and_carry_no_identifiers(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    admin = await make_user(
        db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan", phone="+79990000500"
    )
    group = await make_event(db_session, admin_id=admin.id)
    await make_membership(db_session, event_id=group.id, user_id=user.id)
    await join(db_session, group, 103)
    # пара, задержка рейса, воркер: ряды получают счётчики подбора, выходов и воркера
    await apply(api_client, db_session, group, 42)
    mate = await apply(api_client, db_session, group, 103)
    await patch_trip(
        api_client,
        mate.json()["trip"]["id"],
        {"estimated_arrival": "2026-11-12T05:35:00+00:00"},
        ADMIN_TELEGRAM_ID,
    )
    factory = shared_session_factory(db_session)
    assert await process_next(factory) == JobOutcome.DONE
    await observe_queue(factory)
    await get_app(api_client, group, 42)

    exposition = generate_latest(REGISTRY).decode()

    assert not UUID_TEXT.search(exposition)
    assert not HEX_ID.search(exposition)
    assert "+7999" not in exposition
    assert label_values(PLACEMENTS, "trigger") <= {t.value for t in MatchTrigger}
    assert label_values(PLACEMENTS, "kind") <= {k.value for k in PlacementKind}
    assert label_values("ft_match_duration_seconds_count", "trigger") <= {
        t.value for t in MatchTrigger
    }
    reasons = {r.value for r in AuditReason}
    assert label_values("ft_transfer_exclusions_total", "reason") <= reasons
    assert label_values("ft_transfers_dissolved_total", "reason") <= reasons
    assert label_values("ft_worker_jobs_total", "kind") <= {k.value for k in JobKind}
    assert label_values("ft_worker_jobs_total", "outcome") <= {o.value for o in JobOutcome}
    assert label_values("ft_worker_job_duration_seconds_count", "kind") <= {
        k.value for k in JobKind
    }
    assert label_values("ft_worker_queue_depth", "status") <= {s.value for s in JobStatus}
    # новые метрики действительно получили ряды на этом сценарии
    assert label_values("ft_worker_jobs_total", "outcome") >= {"done"}
    assert label_values("ft_transfer_exclusions_total", "reason") >= {"incompatible_after_change"}


def test_all_new_metrics_are_registered() -> None:
    names = {metric.name for metric in REGISTRY.collect()}

    assert {
        "ft_match_placements",
        "ft_match_duration_seconds",
        "ft_transfer_exclusions",
        "ft_transfers_dissolved",
        "ft_worker_jobs",
        "ft_worker_job_duration_seconds",
        "ft_worker_queue_depth",
        "ft_worker_oldest_job_age_seconds",
        "ft_worker_last_poll_timestamp_seconds",
    } <= names
