"""`reconcile_event`: пересчёт трансферов после правки времени или аэропорта прилёта рейса.

Составы собираем настоящими `match_application`, чтобы в истории были `transfer_created` и
`assert_history_matches_transfers` мог восстановить их. Правку рейса делаем через `Trip`
(`estimated_arrival` или `arrival_location_id`) с flush, как это сделает `update_trip`.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import event as event_stats
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.matching import MatchCandidate, is_compatible
from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    Transfer,
    TransferOrigin,
    TransferStatus,
    Trip,
)
from app.services.audit import SYSTEM, Actor, record
from app.services.matching import (
    ReconcileResult,
    all_departed,
    mark_transfer_departed,
    match_application,
    reconcile_event,
)
from tests.factories import (
    get_location,
    make_application,
    make_event,
    make_transfer,
    make_trip,
)
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows

BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
NOTHING = ReconcileResult(excluded=0, dissolved=0, placed=0)

Snapshot = tuple[
    list[tuple[uuid.UUID, str, uuid.UUID | None, datetime | None]],
    list[tuple[uuid.UUID, str, str]],
    list[int],
]


# ---------------------------------------------------------------- хелперы


async def _app(
    session: AsyncSession,
    event_id: uuid.UUID,
    minutes: int = 0,
    *,
    trip: Trip | None = None,
    **kw: Any,
) -> Application:
    """Заявка на свой рейс через `minutes` после базового либо на общий рейс `trip`."""
    if trip is None:
        trip = await make_trip(
            session, event_id=event_id, scheduled_arrival=BASE + timedelta(minutes=minutes)
        )
    kw.setdefault("baggage_count", 0)
    return await make_application(session, event_id=event_id, trip_id=trip.id, **kw)


async def _trip_of(session: AsyncSession, application: Application) -> Trip:
    trip = await session.get(Trip, application.trip_id)
    assert trip is not None
    return trip


async def _delay(session: AsyncSession, trip: Trip, minutes: int) -> None:
    """Рейс прилетает через `minutes` после базового (расчётное время), как после правки админа."""
    trip.estimated_arrival = BASE + timedelta(minutes=minutes)
    await session.flush()


async def _reconcile(session: AsyncSession, event_id: uuid.UUID, *trips: Trip) -> ReconcileResult:
    return await reconcile_event(
        session, event_id, changed_trip_ids=frozenset(trip.id for trip in trips)
    )


async def _triple(
    session: AsyncSession, event_id: uuid.UUID
) -> tuple[Application, Application, Application, uuid.UUID]:
    """Тройка: двое на общем рейсе и третий на своём, все в одно время."""
    shared = await make_trip(session, event_id=event_id, scheduled_arrival=BASE)
    first = await _app(session, event_id, trip=shared)
    second = await _app(session, event_id, trip=shared)
    third = await _app(session, event_id)
    transfer = await match_application(session, third)
    assert transfer is not None
    for member in (first, second, third):
        assert member.transfer_id == transfer.id
    return first, second, third, transfer.id


async def _snapshot(session: AsyncSession, event_id: uuid.UUID) -> Snapshot:
    applications = await session.execute(
        select(Application.id, Application.status, Application.transfer_id, Application.departed_at)
        .where(Application.event_id == event_id)
        .order_by(Application.id)
    )
    transfers = await session.execute(
        select(Transfer.id, Transfer.status, Transfer.origin)
        .where(Transfer.event_id == event_id)
        .order_by(Transfer.id)
    )
    return (
        [(row.id, row.status, row.transfer_id, row.departed_at) for row in applications],
        [(row.id, row.status, row.origin) for row in transfers],
        [row.id for row in await audit_rows(session, event_id)],
    )


async def _assert_rerun_changes_nothing(
    session: AsyncSession, event_id: uuid.UUID, *trips: Trip
) -> None:
    before = await _snapshot(session, event_id)

    assert await _reconcile(session, event_id, *trips) == NOTHING

    assert await _snapshot(session, event_id) == before


def _brief(row: AuditLog) -> tuple[str, str | None, str]:
    return row.action, row.reason, row.actor_kind


# ---------------------------------------------------------------- состав цел


async def test_shift_of_shared_trip_keeps_composition(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    shared = await make_trip(db_session, event_id=event.id, scheduled_arrival=BASE)
    members = [await _app(db_session, event.id, trip=shared) for _ in range(3)]
    transfer = await match_application(db_session, members[-1])
    assert transfer is not None
    await _delay(db_session, shared, 120)  # все сдвинулись вместе: разброс прилётов тот же
    before = await _snapshot(db_session, event.id)

    result = await _reconcile(db_session, event.id, shared)

    assert result == NOTHING
    assert await _snapshot(db_session, event.id) == before
    await assert_history_matches_transfers(db_session, event.id)


async def test_delay_within_wait_changes_nothing(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    await _app(db_session, event.id, max_wait_minutes=15)
    second = await _app(db_session, event.id, max_wait_minutes=15)
    assert await match_application(db_session, second) is not None
    trip = await _trip_of(db_session, second)
    await _delay(db_session, trip, 10)  # 10 минут при ожидании 15
    before = await _snapshot(db_session, event.id)

    assert await _reconcile(db_session, event.id, trip) == NOTHING

    assert await _snapshot(db_session, event.id) == before


# ---------------------------------------------------------------- исключение


async def test_third_on_other_trip_becomes_incompatible_and_leaves(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    first, second, third, transfer_id = await _triple(db_session, event.id)
    trip = await _trip_of(db_session, third)
    await _delay(db_session, trip, 30)  # ожидание 15 минут

    result = await _reconcile(db_session, event.id, trip)

    assert result == ReconcileResult(excluded=1, dissolved=0, placed=0)
    assert (third.status, third.transfer_id) == (ApplicationStatus.SEARCHING, None)
    for stayed in (first, second):
        assert (stayed.status, stayed.transfer_id) == (ApplicationStatus.ASSIGNED, transfer_id)
    left = (await audit_rows(db_session, event.id))[-1]
    assert _brief(left) == ("transfer_left", "incompatible_after_change", "system")
    assert left.trip_id == trip.id  # по записи видно, какая правка рейса вызвала исключение
    assert left.actor_user_id is None
    assert (left.application_id, left.subject_user_id) == (third.id, third.user_id)
    assert left.transfer_id == transfer_id
    assert left.details == {"from_status": "assigned", "to_status": "searching"}
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, trip)


async def test_arrival_airport_change_excludes_application(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first, second, third, transfer_id = await _triple(db_session, event.id)
    trip = await _trip_of(db_session, third)
    trip.arrival_location_id = (await get_location(db_session, "DME")).id
    await db_session.flush()

    result = await _reconcile(db_session, event.id, trip)

    assert result == ReconcileResult(excluded=1, dissolved=0, placed=0)
    assert (third.status, third.transfer_id) == (ApplicationStatus.SEARCHING, None)
    assert first.transfer_id == second.transfer_id == transfer_id
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, trip)


async def test_pair_becomes_incompatible_and_transfer_is_dissolved(
    db_session: AsyncSession,
) -> None:
    event = await make_event(db_session)
    first = await _app(db_session, event.id)
    second = await _app(db_session, event.id)
    transfer = await match_application(db_session, second)
    assert transfer is not None
    trip = await _trip_of(db_session, second)
    await _delay(db_session, trip, 30)

    result = await _reconcile(db_session, event.id, trip)

    assert result == ReconcileResult(excluded=1, dissolved=1, placed=0)
    for member in (first, second):
        assert (member.status, member.transfer_id) == (ApplicationStatus.SEARCHING, None)
    assert await db_session.get(Transfer, transfer.id, populate_existing=True) is None
    rows = (await audit_rows(db_session, event.id))[-3:]
    assert [_brief(row) for row in rows] == [
        ("transfer_left", "incompatible_after_change", "system"),
        ("transfer_left", "member_left", "system"),
        ("transfer_dissolved", "member_left", "system"),
    ]
    # изменившийся уходит первым, остаток расформированного трансфера — следом
    assert [row.application_id for row in rows[:2]] == [second.id, first.id]
    # все записи одного выхода называют изменённый рейс
    assert [row.trip_id for row in rows] == [trip.id] * 3
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, trip)


async def test_dissolved_pair_members_are_seated_from_search(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    # багаж 1 + 1 заполняет пару, поэтому ждущая заявка с багажом 1 в неё не садится
    first = await _app(db_session, event.id, baggage_count=1)
    second = await _app(db_session, event.id, baggage_count=1)
    pair = await match_application(db_session, second)
    assert pair is not None
    waiting = await _app(db_session, event.id, 5, baggage_count=1)
    assert await match_application(db_session, waiting) is None
    trip = await _trip_of(db_session, second)
    await _delay(db_session, trip, 30)

    result = await _reconcile(db_session, event.id, trip)

    # первая и ждущая собираются в новую пару, вторая (на сдвинутом рейсе) остаётся в поиске
    # две заявки (первая и ждущая) собрались в новую пару: placed считает заявки, не трансферы
    assert result == ReconcileResult(excluded=1, dissolved=1, placed=2)
    assert first.status == waiting.status == ApplicationStatus.ASSIGNED
    assert first.transfer_id == waiting.transfer_id not in (None, pair.id)
    assert (second.status, second.transfer_id) == (ApplicationStatus.SEARCHING, None)
    created = (await audit_rows(db_session, event.id))[-1]
    assert _brief(created) == ("transfer_created", "auto_fill", "system")
    assert created.transfer_id == first.transfer_id
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, trip)


async def test_freed_place_is_taken_by_application_from_search(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first = await _app(db_session, event.id)
    second = await _app(db_session, event.id)
    third = await _app(db_session, event.id)
    transfer = await match_application(db_session, third)
    assert transfer is not None
    waiting = await _app(db_session, event.id)
    assert await match_application(db_session, waiting) is None  # машина полная
    trip = await _trip_of(db_session, third)
    await _delay(db_session, trip, 30)

    result = await _reconcile(db_session, event.id, trip)

    assert result == ReconcileResult(excluded=1, dissolved=0, placed=1)
    assert (third.status, third.transfer_id) == (ApplicationStatus.SEARCHING, None)
    assert (waiting.status, waiting.transfer_id) == (ApplicationStatus.ASSIGNED, transfer.id)
    assert first.transfer_id == second.transfer_id == transfer.id
    left, joined = (await audit_rows(db_session, event.id))[-2:]
    assert _brief(left) == ("transfer_left", "incompatible_after_change", "system")
    assert _brief(joined) == ("transfer_joined", "auto_fill", "system")
    assert joined.application_id == waiting.id
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, trip)


async def test_all_transfers_are_checked_before_filling(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    *_, third, first_transfer = await _triple(db_session, event.id)
    dme = await get_location(db_session, "DME")
    left = await _app(
        db_session,
        event.id,
        trip=await make_trip(
            db_session, event_id=event.id, scheduled_arrival=BASE, arrival_location_id=dme.id
        ),
    )
    right = await _app(
        db_session,
        event.id,
        trip=await make_trip(
            db_session, event_id=event.id, scheduled_arrival=BASE, arrival_location_id=dme.id
        ),
    )
    second_transfer = await match_application(db_session, right)
    assert second_transfer is not None
    third_trip = await _trip_of(db_session, third)
    right_trip = await _trip_of(db_session, right)
    await _delay(db_session, third_trip, 30)
    await _delay(db_session, right_trip, 30)

    result = await _reconcile(db_session, event.id, third_trip, right_trip)

    assert result == ReconcileResult(excluded=2, dissolved=1, placed=0)
    # причин две, и записи не называют ни одну из них
    exclusion_rows = [
        row
        for row in await audit_rows(db_session, event.id)
        if row.reason == "incompatible_after_change"
    ]
    assert len(exclusion_rows) == 2
    assert {row.trip_id for row in exclusion_rows} == {None}
    assert third.status == left.status == right.status == ApplicationStatus.SEARCHING
    transfer_ids = set(
        (await db_session.scalars(select(Transfer.id).where(Transfer.event_id == event.id))).all()
    )
    assert transfer_ids == {first_transfer}
    await assert_history_matches_transfers(db_session, event.id)
    await _assert_rerun_changes_nothing(db_session, event.id, third_trip, right_trip)


async def test_admin_transfer_is_reconciled_too(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    transfer = await make_transfer(db_session, event_id=event.id, origin=TransferOrigin.ADMIN)
    first = await _app(db_session, event.id, transfer_id=transfer.id)
    second = await _app(db_session, event.id, transfer_id=transfer.id)
    record(
        db_session,
        event_id=event.id,
        action=AuditAction.TRANSFER_CREATED,
        actor=Actor(ActorKind.ADMIN, None),
        transfer_id=transfer.id,
        details={"application_ids": [str(first.id), str(second.id)]},
    )
    trip = await _trip_of(db_session, second)
    await _delay(db_session, trip, 30)

    result = await _reconcile(db_session, event.id, trip)

    assert result == ReconcileResult(excluded=1, dissolved=1, placed=0)
    assert first.status == second.status == ApplicationStatus.SEARCHING
    await assert_history_matches_transfers(db_session, event.id)


# ---------------------------------------------------------------- уехавшие


async def test_departed_member_is_not_excluded_and_not_counted(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first, second, third, transfer_id = await _triple(db_session, event.id)
    third.departed_at = BASE
    trip = await _trip_of(db_session, third)
    await _delay(db_session, trip, 60)  # уехавшая заявка теперь несовместима с остальными
    before = await _snapshot(db_session, event.id)

    result = await _reconcile(db_session, event.id, trip)

    assert result == NOTHING
    assert await _snapshot(db_session, event.id) == before
    for member in (first, second, third):
        assert (member.status, member.transfer_id) == (ApplicationStatus.ASSIGNED, transfer_id)


async def test_departed_member_stays_when_another_leaves(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    first, second, third, transfer_id = await _triple(db_session, event.id)
    first.departed_at = BASE
    trip = await _trip_of(db_session, third)
    await _delay(db_session, trip, 30)

    result = await _reconcile(db_session, event.id, trip)

    # из ждущих второй и третья несовместимы: уходит изменившаяся третья, уехавшая остаётся
    assert result == ReconcileResult(excluded=1, dissolved=0, placed=0)
    assert (third.status, third.transfer_id) == (ApplicationStatus.SEARCHING, None)
    assert first.transfer_id == second.transfer_id == transfer_id
    status = await db_session.scalar(
        select(Transfer.status)
        .where(Transfer.id == transfer_id)
        .execution_options(populate_existing=True)
    )
    assert status == TransferStatus.ACTIVE
    await assert_history_matches_transfers(db_session, event.id)


async def test_departed_transfer_is_not_touched(db_session: AsyncSession) -> None:
    event = await make_event(db_session)
    members = await _triple(db_session, event.id)
    transfer_id = members[3]
    trips = []
    for member in members[:3]:
        member.departed_at = BASE
        trips.append(await _trip_of(db_session, member))
    await db_session.flush()
    await mark_transfer_departed(db_session, event.id, transfer_id, SYSTEM)
    for index, trip in enumerate(trips):
        await _delay(db_session, trip, 60 * index)
    before = await _snapshot(db_session, event.id)

    result = await _reconcile(db_session, event.id, *trips)

    assert result == NOTHING
    assert await _snapshot(db_session, event.id) == before
    status = await db_session.scalar(
        select(Transfer.status)
        .where(Transfer.id == transfer_id)
        .execution_options(populate_existing=True)
    )
    assert status == TransferStatus.DEPARTED
    assert {member.status for member in members[:3]} == {ApplicationStatus.ASSIGNED}


# ---------------------------------------------------------------- свойство


@dataclass(frozen=True)
class _Spec:
    """Заявка случайной группы: исходный рейс и, возможно, правка рейса."""

    minutes: int
    airport: str
    companion: bool
    baggage: int
    wait: int
    departed: bool
    new_minutes: int | None
    new_airport: str | None


_SPECS = st.builds(
    _Spec,
    minutes=st.integers(0, 40),
    airport=st.sampled_from(["SVO", "DME"]),
    companion=st.booleans(),
    baggage=st.integers(0, 2),
    wait=st.sampled_from([0, 10, 20, 40]),
    departed=st.sampled_from([False, False, False, True]),
    new_minutes=st.one_of(st.none(), st.integers(-30, 80)),
    new_airport=st.one_of(st.none(), st.sampled_from(["SVO", "DME"])),
)


async def _check_second_reconcile_is_noop(session: AsyncSession, specs: list[_Spec]) -> None:
    event = await make_event(session)
    members: list[tuple[Application, Trip, _Spec]] = []
    for spec in specs:
        trip = await make_trip(
            session,
            event_id=event.id,
            scheduled_arrival=BASE + timedelta(minutes=spec.minutes),
            arrival_location_id=(await get_location(session, spec.airport)).id,
        )
        application = await _app(
            session,
            event.id,
            trip=trip,
            with_companion=spec.companion,
            baggage_count=spec.baggage,
            max_wait_minutes=spec.wait,
        )
        await match_application(session, application)  # как PUT: заявки подаются по очереди
        members.append((application, trip, spec))
    for application, _, spec in members:
        if spec.departed:
            application.departed_at = BASE
    await session.flush()
    for transfer_id in {app.transfer_id for app, _, _ in members if app.transfer_id is not None}:
        if await all_departed(session, transfer_id):
            await mark_transfer_departed(session, event.id, transfer_id, SYSTEM)

    changed: set[uuid.UUID] = set()
    for _, trip, spec in members:
        if spec.new_minutes is not None:
            trip.estimated_arrival = BASE + timedelta(minutes=spec.new_minutes)
            changed.add(trip.id)
        if spec.new_airport is not None:
            trip.arrival_location_id = (await get_location(session, spec.new_airport)).id
            changed.add(trip.id)
    await session.flush()

    first = await reconcile_event(session, event.id, changed_trip_ids=frozenset(changed))
    event_stats(
        f"excluded={first.excluded > 0}, dissolved={first.dissolved > 0}, placed={first.placed > 0}"
    )

    await assert_history_matches_transfers(session, event.id)
    await _assert_waiting_members_are_compatible(session, event.id)
    before = await _snapshot(session, event.id)
    assert await reconcile_event(session, event.id, changed_trip_ids=frozenset(changed)) == NOTHING
    assert await _snapshot(session, event.id) == before


async def _assert_waiting_members_are_compatible(
    session: AsyncSession, event_id: uuid.UUID
) -> None:
    """В каждом активном трансфере неуехавшие по-прежнему помещаются в одну машину."""
    rows = (
        await session.execute(
            select(Application, Trip)
            .join(Trip, Trip.id == Application.trip_id)
            .join(Transfer, Transfer.id == Application.transfer_id)
            .where(
                Application.event_id == event_id,
                Transfer.status == TransferStatus.ACTIVE,
                Application.departed_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    cars: dict[uuid.UUID | None, list[MatchCandidate]] = {}
    for application, trip in rows:
        cars.setdefault(application.transfer_id, []).append(
            MatchCandidate(
                id=application.id,
                arrival_location_id=trip.arrival_location_id,
                arrival_at=trip.effective_arrival,
                passengers=2 if application.with_companion else 1,
                baggage=application.baggage_count,
                max_wait=timedelta(minutes=application.max_wait_minutes),
            )
        )
    for car in cars.values():
        assert is_compatible(car)


async def _run_isolated(url: str, specs: list[_Spec]) -> None:
    """Один пример свойства в своём цикле событий, соединении и откатываемой транзакции."""
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            try:
                await _check_second_reconcile_is_noop(session, specs)
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(specs=st.lists(_SPECS, min_size=2, max_size=6))
def test_second_reconcile_changes_nothing_on_random_groups(
    migrated_database: str, specs: list[_Spec]
) -> None:
    asyncio.run(_run_isolated(migrated_database, specs))
