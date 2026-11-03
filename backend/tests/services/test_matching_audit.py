"""История в `match_application`, `fill_event` и `leave_transfer`: какие записи, от кого, почему."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    AuditReason,
)
from app.observability.metrics import MatchTrigger
from app.services.audit import SYSTEM, Actor
from app.services.matching import FillResult, fill_event, leave_transfer, match_application
from tests.factories import make_application, make_event, make_transfer, make_trip
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)


async def _app(
    session: AsyncSession, event_id: uuid.UUID, minutes: int = 0, **kw: Any
) -> Application:
    trip = await make_trip(
        session, event_id=event_id, scheduled_arrival=BASE + timedelta(minutes=minutes)
    )
    return await make_application(
        session, event_id=event_id, trip_id=trip.id, baggage_count=0, **kw
    )


def _brief(row: AuditLog) -> tuple[str, str | None, str]:
    return row.action, row.reason, row.actor_kind


async def test_match_writes_transfer_created_from_system(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first = await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)

    transfer = await match_application(db_session, second)

    assert transfer is not None
    (row,) = await audit_rows(db_session, event.id)
    assert _brief(row) == ("transfer_created", "auto_match", "system")
    assert row.actor_user_id is None
    assert (row.subject_user_id, row.application_id) == (None, None)
    assert row.transfer_id == transfer.id
    # порядок группы: по времени прилёта
    assert row.details == {"application_ids": [str(first.id), str(second.id)]}


async def test_match_without_group_writes_nothing(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    lonely = await _app(db_session, event.id)

    assert await match_application(db_session, lonely) is None

    assert await audit_rows(db_session, event.id) == []


async def test_leave_pair_passes_actor_and_reason_and_writes_dissolution(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    leaving = await _app(db_session, event.id)
    staying = await _app(db_session, event.id, 5)
    transfer = await match_application(db_session, staying)
    assert transfer is not None
    await db_session.refresh(leaving)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.SOLO,
        actor=SYSTEM,
        reason=AuditReason.INCOMPATIBLE_AFTER_CHANGE,
    )

    created, left, remaining_left, dissolved = await audit_rows(db_session, event.id)
    assert created.action == AuditAction.TRANSFER_CREATED

    assert _brief(left) == ("transfer_left", "incompatible_after_change", "system")
    assert (left.application_id, left.subject_user_id) == (leaving.id, leaving.user_id)
    assert left.transfer_id == transfer.id
    assert left.details == {"from_status": "assigned", "to_status": "solo"}

    assert _brief(remaining_left) == ("transfer_left", "member_left", "system")
    assert (remaining_left.application_id, remaining_left.subject_user_id) == (
        staying.id,
        staying.user_id,
    )
    assert remaining_left.transfer_id == transfer.id
    assert remaining_left.details == {"from_status": "assigned", "to_status": "searching"}

    assert dissolved.action == AuditAction.TRANSFER_DISSOLVED
    assert dissolved.transfer_id == transfer.id
    assert dissolved.details == {"application_ids": [str(staying.id)]}
    assert (dissolved.application_id, dissolved.subject_user_id) == (None, None)
    assert [row.id for row in (created, left, remaining_left, dissolved)] == sorted(
        row.id for row in (created, left, remaining_left, dissolved)
    )
    await assert_history_matches_transfers(db_session, event.id)


async def test_leave_writes_participant_as_actor_for_every_record(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    leaving = await _app(db_session, event.id)
    staying = await _app(db_session, event.id, 5)
    await match_application(db_session, staying)
    await db_session.refresh(leaving)
    actor = Actor(ActorKind.PARTICIPANT, leaving.user_id)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=actor,
        reason=AuditReason.CANCEL,
    )

    rows = (await audit_rows(db_session, event.id))[1:]
    assert [_brief(row) for row in rows] == [
        ("transfer_left", "cancel", "participant"),
        ("transfer_left", "member_left", "participant"),
        ("transfer_dissolved", "member_left", "participant"),
    ]
    assert {row.actor_user_id for row in rows} == {leaving.user_id}
    assert rows[0].details == {"from_status": "assigned", "to_status": "cancelled"}


async def test_leave_triple_writes_only_the_leaver(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id)
    leaving = await _app(db_session, event.id, transfer_id=transfer.id)
    await _app(db_session, event.id, transfer_id=transfer.id)
    await _app(db_session, event.id, transfer_id=transfer.id)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.SOLO,
        actor=Actor(ActorKind.PARTICIPANT, leaving.user_id),
        reason=AuditReason.GO_SOLO,
    )

    (row,) = await audit_rows(db_session, event.id)
    assert _brief(row) == ("transfer_left", "go_solo", "participant")
    assert (row.application_id, row.transfer_id) == (leaving.id, transfer.id)


async def test_leave_when_remaining_all_departed_writes_transfer_departed(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id)
    leaving = await _app(db_session, event.id, transfer_id=transfer.id)
    await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)
    await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)
    actor = Actor(ActorKind.PARTICIPANT, leaving.user_id)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.SOLO,
        actor=actor,
        reason=AuditReason.GO_SOLO,
    )

    left, departed = await audit_rows(db_session, event.id)
    assert left.action == AuditAction.TRANSFER_LEFT
    assert _brief(departed) == ("transfer_departed", "all_departed", "participant")
    assert departed.actor_user_id == leaving.user_id
    assert departed.transfer_id == transfer.id
    assert departed.details == {}


async def test_leave_pair_with_departed_remaining_writes_solo_not_searching(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id)
    leaving = await _app(db_session, event.id, transfer_id=transfer.id)
    departed = await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=SYSTEM,
        reason=AuditReason.INCOMPATIBLE_AFTER_CHANGE,
    )

    _, remaining_left, dissolved = await audit_rows(db_session, event.id)
    assert remaining_left.application_id == departed.id
    assert remaining_left.details == {"from_status": "assigned", "to_status": "solo"}
    assert dissolved.details == {"application_ids": [str(departed.id)]}


async def test_leave_pair_then_fill_writes_transfer_created_after_dissolution(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    leaving = await _app(db_session, event.id)
    staying = await _app(db_session, event.id, 5)
    first = await match_application(db_session, staying)
    assert first is not None
    free = await _app(db_session, event.id, 6)
    await db_session.refresh(leaving)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=Actor(ActorKind.PARTICIPANT, leaving.user_id),
        reason=AuditReason.CANCEL,
    )

    # расформирование само нового подбора не запускает
    assert [row.action for row in await audit_rows(db_session, event.id)] == [
        "transfer_created",
        "transfer_left",
        "transfer_left",
        "transfer_dissolved",
    ]

    await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE)

    rows = await audit_rows(db_session, event.id)
    assert [row.action for row in rows][-1] == "transfer_created"
    rematch = rows[-1]
    assert _brief(rematch) == ("transfer_created", "auto_fill", "system")
    assert rematch.transfer_id not in (None, first.id)
    assert rematch.details == {"application_ids": [str(staying.id), str(free.id)]}
    await assert_history_matches_transfers(db_session, event.id)


async def test_match_joining_open_transfer_writes_transfer_joined(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    pair = await match_application(db_session, second)
    assert pair is not None
    third = await _app(db_session, event.id, 6)

    transfer = await match_application(db_session, third)

    assert transfer is not None
    assert transfer.id == pair.id
    assert (third.status, third.transfer_id) == (ApplicationStatus.ASSIGNED, pair.id)
    created, joined = await audit_rows(db_session, event.id)
    assert created.action == AuditAction.TRANSFER_CREATED
    assert _brief(joined) == ("transfer_joined", "auto_match", "system")
    assert joined.actor_user_id is None
    assert (joined.application_id, joined.subject_user_id) == (third.id, third.user_id)
    assert joined.transfer_id == pair.id
    assert joined.details == {}
    await assert_history_matches_transfers(db_session, event.id)


async def test_match_passes_reason_to_both_kinds_of_records(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    assert await match_application(db_session, second, reason=AuditReason.AUTO_FILL) is not None
    third = await _app(db_session, event.id, 6)

    assert await match_application(db_session, third, reason=AuditReason.AUTO_FILL) is not None

    created, joined = await audit_rows(db_session, event.id)
    assert _brief(created) == ("transfer_created", "auto_fill", "system")
    assert _brief(joined) == ("transfer_joined", "auto_fill", "system")


async def test_fill_event_writes_auto_fill_records_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    pair = await match_application(db_session, second)
    assert pair is not None
    newcomer = await _app(db_session, event.id, 6)
    await _app(db_session, event.id, 600)  # ни с кем не совместима

    assert await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE) == FillResult(
        joined=1, created=0
    )

    rows = await audit_rows(db_session, event.id)
    assert [_brief(row) for row in rows] == [
        ("transfer_created", "auto_match", "system"),
        ("transfer_joined", "auto_fill", "system"),
    ]
    assert (rows[-1].application_id, rows[-1].transfer_id) == (newcomer.id, pair.id)
    before = [row.id for row in rows]

    assert await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE) == FillResult(
        joined=0, created=0
    )

    assert [row.id for row in await audit_rows(db_session, event.id)] == before
    await assert_history_matches_transfers(db_session, event.id)
