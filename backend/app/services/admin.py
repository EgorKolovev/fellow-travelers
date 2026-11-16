import uuid
from collections.abc import Collection, Sequence
from typing import NamedTuple

from sqlalchemy import ColumnElement, Integer, case, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Application,
    ApplicationStatus,
    Membership,
    Transfer,
    Trip,
    TripStatus,
    User,
)
from app.schemas.admin import Counters
from app.services.applications import WITH_TRIP
from app.services.transfers import load_transfers


async def get_counters(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    trip_ids: Collection[uuid.UUID] | None = None,
) -> Counters:
    """Счётчики по группе одним запросом; считаются люди (спутник = 2), не заявки.

    `trip_ids` ограничивает заявки этими рейсами (пустой набор — нули); `None` — вся группа.
    """
    people = case((Application.with_companion, 2), else_=1)
    active = Application.status != ApplicationStatus.CANCELLED

    def total(condition: ColumnElement[bool]) -> ColumnElement[int]:
        return cast(func.coalesce(func.sum(case((condition, people), else_=0)), 0), Integer)

    stmt = select(
        total(active).label("expected"),
        total(Application.status == ApplicationStatus.SEARCHING).label("searching"),
        total(Application.status == ApplicationStatus.ASSIGNED).label("assigned"),
        total(Application.status == ApplicationStatus.SOLO).label("solo"),
        total(active & Application.at_meeting_point).label("at_meeting_point"),
        total(Application.departed_at.is_not(None)).label("departed"),
    ).where(Application.event_id == event_id)
    if trip_ids is not None:
        stmt = stmt.where(Application.trip_id.in_(trip_ids))
    row = (await session.execute(stmt)).one()
    return Counters(**row._asdict())


async def list_participants(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    statuses: Sequence[ApplicationStatus] | None,
    trip_id: uuid.UUID | None,
) -> list[tuple[User, Application | None]]:
    """Участники группы с заявками (если есть). Только чтение, фиксированное число запросов.

    Любой фильтр исключает участников без заявки.
    """
    stmt = (
        select(User, Application)
        .join(Membership, Membership.user_id == User.id)
        .outerjoin(
            Application,
            (Application.user_id == User.id) & (Application.event_id == event_id),
        )
        .outerjoin(Trip, Trip.id == Application.trip_id)
        .where(Membership.event_id == event_id)
        .options(*WITH_TRIP)
        .order_by(
            Application.id.is_(None),
            Trip.effective_arrival,
            User.first_name,
            User.last_name,
            User.id,
        )
        .execution_options(populate_existing=True)
    )
    if statuses:
        stmt = stmt.where(Application.status.in_(statuses))
    if trip_id is not None:
        stmt = stmt.where(Application.trip_id == trip_id)
    return [(user, application) for user, application in (await session.execute(stmt)).all()]


async def list_transfers(session: AsyncSession, event_id: uuid.UUID) -> list[Transfer]:
    """Все трансферы группы: по самому раннему прибытию участников, затем created_at, id."""
    ordered = (
        select(Transfer.id)
        .outerjoin(Application, Application.transfer_id == Transfer.id)
        .outerjoin(Trip, Trip.id == Application.trip_id)
        .where(Transfer.event_id == event_id)
        .group_by(Transfer.id, Transfer.created_at)
        .order_by(func.min(Trip.effective_arrival), Transfer.created_at, Transfer.id)
    )
    ids = list((await session.scalars(ordered)).all())
    by_id = {t.id: t for t in await load_transfers(session, ids)}
    return [by_id[i] for i in ids]


class BoardRow(NamedTuple):
    trip: Trip
    people: int
    application_ids: list[uuid.UUID]


async def get_board(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    trip_statuses: Sequence[TripStatus] | None,
) -> tuple[Counters, list[BoardRow]]:
    """Табло: рейсы с активными заявками по расчётному прибытию, счётчики по ним.

    Фиксированное число запросов: рейсы (+ их локации), заявки всех рейсов, счётчики.
    """
    active = Application.status != ApplicationStatus.CANCELLED
    trips_stmt = (
        select(Trip)
        .where(
            Trip.event_id == event_id,
            exists().where(Application.trip_id == Trip.id, active),
        )
        .options(selectinload(Trip.departure_location), selectinload(Trip.arrival_location))
        .order_by(Trip.effective_arrival, Trip.id)
    )
    if trip_statuses:
        trips_stmt = trips_stmt.where(Trip.status.in_(trip_statuses))
    trips = list((await session.scalars(trips_stmt)).all())
    trip_ids = [t.id for t in trips]

    apps_stmt = (
        select(Application.id, Application.trip_id, Application.with_companion)
        .where(Application.trip_id.in_(trip_ids), active)
        .order_by(Application.created_at, Application.id)
    )
    by_trip: dict[uuid.UUID, list[tuple[uuid.UUID, bool]]] = {t.id: [] for t in trips}
    for app_id, trip_id, with_companion in (await session.execute(apps_stmt)).all():
        by_trip[trip_id].append((app_id, with_companion))

    rows = [
        BoardRow(
            trip=t,
            people=sum(2 if c else 1 for _, c in by_trip[t.id]),
            application_ids=[i for i, _ in by_trip[t.id]],
        )
        for t in trips
    ]
    counters = await get_counters(session, event_id, trip_ids=trip_ids)
    return counters, rows
