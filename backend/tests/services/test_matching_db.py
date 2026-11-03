import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditLog,
    AuditReason,
    Transfer,
    TransferStatus,
)
from app.observability.metrics import MatchTrigger
from app.services.audit import Actor
from app.services.matching import (
    FillResult,
    event_lock_key,
    fill_event,
    leave_transfer,
    lock_event,
    match_application,
)
from tests.factories import make_application, make_event, make_transfer, make_trip

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
PARTICIPANT = Actor(ActorKind.PARTICIPANT, None)


@pytest.mark.parametrize(
    "event_id",
    [
        uuid.UUID(int=0),
        uuid.UUID(int=(1 << 128) - 1),
        uuid.UUID(int=1 << 127),
        uuid.UUID(int=(1 << 127) - 1),
        uuid.UUID("6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"),
        *(uuid.uuid4() for _ in range(50)),
    ],
)
def test_lock_key_is_deterministic_int64(event_id: uuid.UUID) -> None:
    key = event_lock_key(event_id)

    assert key == event_lock_key(uuid.UUID(str(event_id)))
    assert -(1 << 63) <= key < (1 << 63)


def test_lock_key_differs_between_events() -> None:
    assert event_lock_key(uuid.UUID(int=1 << 64)) != event_lock_key(uuid.UUID(int=2 << 64))


async def test_lock_event_executes(db_session: AsyncSession) -> None:
    event = await make_event(db_session)

    await lock_event(db_session, event.id)
    await lock_event(db_session, event.id)  # повторно в той же транзакции — не блокирует себя


async def _app(
    session: AsyncSession, event_id: uuid.UUID, minutes: int = 0, **kw: object
) -> Application:
    trip = await make_trip(
        session, event_id=event_id, scheduled_arrival=BASE + timedelta(minutes=minutes)
    )
    return await make_application(
        session, event_id=event_id, trip_id=trip.id, baggage_count=0, **kw
    )


async def test_match_ignores_non_searching_departed_and_assigned(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    solo = await _app(db_session, event.id, status=ApplicationStatus.SOLO)
    departed = await _app(db_session, event.id, departed_at=BASE)
    transfer = await make_transfer(db_session, event_id=event.id)
    assigned = await _app(db_session, event.id, transfer_id=transfer.id)

    assert await match_application(db_session, solo) is None
    assert await match_application(db_session, departed) is None
    assert await match_application(db_session, assigned) is None


async def test_match_creates_transfer_for_pair(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first = await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)

    transfer = await match_application(db_session, second)

    assert transfer is not None
    await db_session.refresh(first)
    assert first.status == second.status == ApplicationStatus.ASSIGNED
    assert first.transfer_id == second.transfer_id == transfer.id


async def test_leave_pair_removes_transfer_and_frees_remaining(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    leaving = await _app(db_session, event.id)
    staying = await _app(db_session, event.id, 5)
    transfer = await match_application(db_session, staying)
    assert transfer is not None
    await db_session.refresh(leaving)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=PARTICIPANT,
        reason=AuditReason.CANCEL,
    )

    await db_session.refresh(staying)
    assert leaving.status == ApplicationStatus.CANCELLED
    assert leaving.transfer_id is None
    assert staying.status == ApplicationStatus.SEARCHING
    assert staying.transfer_id is None
    count = await db_session.scalar(select(func.count()).select_from(Transfer))
    assert count == 0


async def test_leave_pair_does_not_rematch_remaining_by_itself(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    leaving = await _app(db_session, event.id)
    staying = await _app(db_session, event.id, 5)
    transfer = await match_application(db_session, staying)
    assert transfer is not None
    free = await _app(db_session, event.id, 6)
    await db_session.refresh(leaving)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=PARTICIPANT,
        reason=AuditReason.CANCEL,
    )

    # новое размещение ищет сценарий через fill_event, а не расформирование
    assert staying.status == free.status == ApplicationStatus.SEARCHING
    assert staying.transfer_id is None
    assert free.transfer_id is None


async def test_leave_when_remaining_all_departed_marks_transfer_departed(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id)
    leaving = await _app(db_session, event.id, transfer_id=transfer.id)
    await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)
    await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.SOLO,
        actor=PARTICIPANT,
        reason=AuditReason.GO_SOLO,
    )

    status = await db_session.scalar(
        select(Transfer.status)
        .where(Transfer.id == transfer.id)
        .execution_options(populate_existing=True)
    )
    assert status == TransferStatus.DEPARTED


async def test_leave_without_transfer_is_noop(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    application = await _app(db_session, event.id)

    await leave_transfer(
        db_session,
        application,
        new_status=ApplicationStatus.SOLO,
        actor=PARTICIPANT,
        reason=AuditReason.GO_SOLO,
    )

    assert application.status == ApplicationStatus.SEARCHING
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_leave_pair_with_departed_remaining_makes_it_solo(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id)
    leaving = await _app(db_session, event.id, transfer_id=transfer.id)
    departed = await _app(db_session, event.id, transfer_id=transfer.id, departed_at=BASE)
    await _app(db_session, event.id, 1)  # свободная совместимая заявка не должна подтянуться

    await leave_transfer(
        db_session,
        leaving,
        new_status=ApplicationStatus.CANCELLED,
        actor=PARTICIPANT,
        reason=AuditReason.CANCEL,
    )

    assert departed.status == ApplicationStatus.SOLO
    assert departed.transfer_id is None
    count = await db_session.scalar(select(func.count()).select_from(Transfer))
    assert count == 0


async def test_fill_event_joins_open_transfer_and_assembles_new_group(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    pair = await match_application(db_session, second)
    assert pair is not None
    joiner = await _app(db_session, event.id, 6)
    early = await _app(db_session, event.id, 100)
    late = await _app(db_session, event.id, 105)
    alone = await _app(db_session, event.id, 1000)

    result = await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE)

    # заявки, а не трансферы: одна села в пару, две собрали новую
    assert result == FillResult(joined=1, created=2)
    assert result.placed == 3
    assert (joiner.status, joiner.transfer_id) == (ApplicationStatus.ASSIGNED, pair.id)
    assert early.status == late.status == ApplicationStatus.ASSIGNED
    assert early.transfer_id == late.transfer_id not in (None, pair.id)
    assert (alone.status, alone.transfer_id) == (ApplicationStatus.SEARCHING, None)


async def test_fill_event_is_idempotent(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id)
    second = await _app(db_session, event.id, 5)
    assert await match_application(db_session, second) is not None
    await _app(db_session, event.id, 6)
    await _app(db_session, event.id, 100)
    await _app(db_session, event.id, 105)
    await _app(db_session, event.id, 1000)
    assert await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE) == FillResult(
        joined=1, created=2
    )

    async def snapshot() -> list[tuple[uuid.UUID, ApplicationStatus, uuid.UUID | None]]:
        rows = await db_session.execute(
            select(Application.id, Application.status, Application.transfer_id)
            .where(Application.event_id == event.id)
            .order_by(Application.id)
            .execution_options(populate_existing=True)
        )
        return [(row.id, row.status, row.transfer_id) for row in rows]

    before = (await snapshot(), await db_session.scalar(select(func.count()).select_from(AuditLog)))

    assert await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE) == FillResult(
        joined=0, created=0
    )

    after = (await snapshot(), await db_session.scalar(select(func.count()).select_from(AuditLog)))
    assert after == before


async def test_fill_event_ignores_unfit_statuses_and_other_events(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id, status=ApplicationStatus.SOLO)
    await _app(db_session, event.id, status=ApplicationStatus.CANCELLED)
    await _app(db_session, event.id, departed_at=BASE)
    await _app(db_session, event.id)  # единственная ждущая
    other = await make_event(db_session)
    other_first = await _app(db_session, other.id)
    other_second = await _app(db_session, other.id, 5)

    assert await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE) == FillResult(
        joined=0, created=0
    )

    assert other_first.status == other_second.status == ApplicationStatus.SEARCHING
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Transfer)) == 0


async def test_fill_result_counts_applications_in_a_new_triple(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    for minutes in (0, 3, 5):
        await _app(db_session, event.id, minutes)

    result = await fill_event(db_session, event.id, trigger=MatchTrigger.LEAVE)

    # один новый трансфер из трёх заявок
    assert result == FillResult(joined=0, created=3)
    assert result.placed == 3
    assert await db_session.scalar(select(func.count()).select_from(Transfer)) == 1
