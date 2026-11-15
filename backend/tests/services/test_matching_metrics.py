"""Метрики подбора: что считается, с какими лейблами и когда (только после commit)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActorKind, Application, ApplicationStatus, AuditReason, Trip
from app.observability.metrics import MatchTrigger, PlacementKind
from app.services.audit import Actor
from app.services.matching import (
    FillResult,
    fill_event,
    leave_transfer,
    match_application,
    reconcile_event,
)
from tests.factories import make_application, make_event, make_trip
from tests.helpers.metrics import sample, total

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
PLACEMENTS = "ft_match_placements_total"
PARTICIPANT = Actor(ActorKind.PARTICIPANT, None)


async def _app(
    session: AsyncSession, event_id: uuid.UUID, minutes: int = 0, **kw: Any
) -> Application:
    trip = await make_trip(
        session, event_id=event_id, scheduled_arrival=BASE + timedelta(minutes=minutes)
    )
    kw.setdefault("baggage_count", 0)
    return await make_application(session, event_id=event_id, trip_id=trip.id, **kw)


def placements(trigger: MatchTrigger, kind: PlacementKind) -> float:
    return sample("ft_match_placements_total", trigger=trigger.value, kind=kind.value)


def durations(trigger: MatchTrigger) -> float:
    return sample("ft_match_duration_seconds_count", trigger=trigger.value)


def exclusions(reason: AuditReason) -> float:
    return sample("ft_transfer_exclusions_total", reason=reason.value)


def dissolved(reason: AuditReason) -> float:
    return sample("ft_transfers_dissolved_total", reason=reason.value)


# ---------------------------------------------------------------- размещения


async def test_new_pair_is_counted_after_commit(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    pairs = placements(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR)
    timed = durations(MatchTrigger.APPLICATION)

    assert await match_application(db_session, second) is not None

    # пока сценарий не закоммичен, счётчики стоят
    assert placements(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR) == pairs
    assert durations(MatchTrigger.APPLICATION) == timed
    await db_session.commit()
    assert placements(MatchTrigger.APPLICATION, PlacementKind.NEW_PAIR) == pairs + 1
    assert durations(MatchTrigger.APPLICATION) == timed + 1


async def test_new_triple_is_counted(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    await _app(db_session, event.id, 3)
    third = await _app(db_session, event.id, 5)
    triples = placements(MatchTrigger.APPLICATION, PlacementKind.NEW_TRIPLE)

    await match_application(db_session, third)
    await db_session.commit()

    assert placements(MatchTrigger.APPLICATION, PlacementKind.NEW_TRIPLE) == triples + 1


async def test_join_is_counted_with_trigger(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    pair = await match_application(db_session, await _app(db_session, event.id, 5))
    assert pair is not None
    third = await _app(db_session, event.id, 6)
    joins = placements(MatchTrigger.LEAVE, PlacementKind.JOIN)
    others = placements(MatchTrigger.APPLICATION, PlacementKind.JOIN)

    await match_application(db_session, third, trigger=MatchTrigger.LEAVE)
    await db_session.commit()

    assert placements(MatchTrigger.LEAVE, PlacementKind.JOIN) == joins + 1
    assert placements(MatchTrigger.APPLICATION, PlacementKind.JOIN) == others


async def test_application_left_waiting_is_counted_as_none(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    lonely = await _app(db_session, event.id)
    waiting = placements(MatchTrigger.APPLICATION, PlacementKind.NONE)
    timed = durations(MatchTrigger.APPLICATION)

    assert await match_application(db_session, lonely) is None
    await db_session.commit()

    assert placements(MatchTrigger.APPLICATION, PlacementKind.NONE) == waiting + 1
    assert durations(MatchTrigger.APPLICATION) == timed + 1


async def test_ineligible_application_is_not_a_match_attempt(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    solo = await _app(db_session, event.id, status=ApplicationStatus.SOLO)
    before = total("ft_match_placements_total")

    assert await match_application(db_session, solo) is None
    await db_session.commit()

    assert total("ft_match_placements_total") == before


async def test_rolled_back_match_is_not_counted(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    before = (total("ft_match_placements_total"), total("ft_match_duration_seconds_count"))

    await match_application(db_session, second)
    await db_session.rollback()
    await db_session.commit()

    assert (total("ft_match_placements_total"), total("ft_match_duration_seconds_count")) == before


@pytest.mark.parametrize("trigger", [MatchTrigger.LEAVE, MatchTrigger.RECONCILE])
async def test_fill_event_counts_only_real_placements(
    db_session: AsyncSession, trigger: MatchTrigger
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    await _app(db_session, event.id, 5)
    for index in range(1, 21):
        await _app(db_session, event.id, 1000 * index)  # ни с кем не совместимы
    pairs = placements(trigger, PlacementKind.NEW_PAIR)
    timed = durations(trigger)
    before = total(PLACEMENTS)

    result = await fill_event(db_session, event.id, trigger=trigger)
    await db_session.commit()

    # пара собрана; двадцать отказов в метрики не попадают: они не размещения
    assert result == FillResult(joined=0, created=2)
    assert placements(trigger, PlacementKind.NEW_PAIR) == pairs + 1
    assert placements(trigger, PlacementKind.NONE) == 0
    assert total(PLACEMENTS) == before + 1
    assert durations(trigger) == timed + 1


async def test_fill_event_with_nothing_to_place_counts_nothing(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    for index in range(10):
        await _app(db_session, event.id, 1000 * index)
    before = (total(PLACEMENTS), total("ft_match_duration_seconds_count"))

    await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE)
    await db_session.commit()

    assert (total(PLACEMENTS), total("ft_match_duration_seconds_count")) == before


async def test_single_match_without_result_counts_none_only_for_application(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    lonely = await _app(db_session, event.id)
    by_user = placements(MatchTrigger.APPLICATION, PlacementKind.NONE)
    by_leave = placements(MatchTrigger.LEAVE, PlacementKind.NONE)

    assert await match_application(db_session, lonely, trigger=MatchTrigger.LEAVE) is None
    await db_session.commit()
    assert placements(MatchTrigger.LEAVE, PlacementKind.NONE) == by_leave

    assert await match_application(db_session, lonely) is None
    await db_session.commit()
    assert placements(MatchTrigger.APPLICATION, PlacementKind.NONE) == by_user + 1


# ---------------------------------------------------------------- выходы и расформирование


async def _pair(session: AsyncSession) -> tuple[uuid.UUID, Application, Application]:
    event = await make_event(session)
    first = await _app(session, event.id)
    second = await _app(session, event.id, 5)
    assert await match_application(session, second) is not None
    await session.commit()
    return event.id, first, second


@pytest.mark.parametrize("reason", [AuditReason.GO_SOLO, AuditReason.CANCEL])
async def test_leaving_pair_counts_exclusions_and_dissolution(
    db_session: AsyncSession, reason: AuditReason
) -> None:
    _, first, _ = await _pair(db_session)
    left = exclusions(reason)
    remaining = exclusions(AuditReason.MEMBER_LEFT)
    gone = dissolved(reason)

    await leave_transfer(
        db_session,
        first,
        new_status=ApplicationStatus.SOLO,
        actor=PARTICIPANT,
        reason=reason,
    )

    assert exclusions(reason) == left
    await db_session.commit()
    assert exclusions(reason) == left + 1
    assert exclusions(AuditReason.MEMBER_LEFT) == remaining + 1
    assert dissolved(reason) == gone + 1


async def test_leaving_triple_counts_only_the_leaver(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    await _app(db_session, event.id, 3)
    third = await _app(db_session, event.id, 5)
    await match_application(db_session, third)
    await db_session.commit()
    left = exclusions(AuditReason.CANCEL)
    remaining = exclusions(AuditReason.MEMBER_LEFT)
    gone = total("ft_transfers_dissolved_total")

    await leave_transfer(
        db_session,
        third,
        new_status=ApplicationStatus.CANCELLED,
        actor=PARTICIPANT,
        reason=AuditReason.CANCEL,
    )
    await db_session.commit()

    assert exclusions(AuditReason.CANCEL) == left + 1
    assert exclusions(AuditReason.MEMBER_LEFT) == remaining
    assert total("ft_transfers_dissolved_total") == gone


async def test_rolled_back_leave_is_not_counted(db_session: AsyncSession) -> None:
    _, first, _ = await _pair(db_session)
    before = (total("ft_transfer_exclusions_total"), total("ft_transfers_dissolved_total"))

    await leave_transfer(
        db_session,
        first,
        new_status=ApplicationStatus.SOLO,
        actor=PARTICIPANT,
        reason=AuditReason.GO_SOLO,
    )
    await db_session.rollback()
    await db_session.commit()

    assert (
        total("ft_transfer_exclusions_total"),
        total("ft_transfers_dissolved_total"),
    ) == before


# ---------------------------------------------------------------- пересчёт


async def test_reconcile_counts_exclusion_dissolution_and_reconcile_placements(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    # багаж 1 + 1 заполняет пару: ждущая заявка с багажом 1 в неё не садится
    await _app(db_session, event.id, baggage_count=1)
    second = await _app(db_session, event.id, baggage_count=1)
    assert await match_application(db_session, second) is not None
    waiting = await _app(db_session, event.id, 5, baggage_count=1)
    assert await match_application(db_session, waiting) is None
    await db_session.commit()
    trip = await db_session.get(Trip, second.trip_id)
    assert trip is not None
    trip.estimated_arrival = BASE + timedelta(minutes=30)
    await db_session.flush()
    excluded = exclusions(AuditReason.INCOMPATIBLE_AFTER_CHANGE)
    dissolutions = dissolved(AuditReason.INCOMPATIBLE_AFTER_CHANGE)
    pairs = placements(MatchTrigger.RECONCILE, PlacementKind.NEW_PAIR)

    result = await reconcile_event(db_session, event.id, changed_trip_ids=frozenset({trip.id}))
    await db_session.commit()

    assert (result.excluded, result.dissolved, result.placed) == (1, 1, 2)
    assert exclusions(AuditReason.INCOMPATIBLE_AFTER_CHANGE) == excluded + 1
    assert dissolved(AuditReason.INCOMPATIBLE_AFTER_CHANGE) == dissolutions + 1
    assert placements(MatchTrigger.RECONCILE, PlacementKind.NEW_PAIR) == pairs + 1
