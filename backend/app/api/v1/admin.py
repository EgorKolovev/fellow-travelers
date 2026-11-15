import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.models import ApplicationStatus
from app.schemas.admin import AdminParticipant, AdminParticipants
from app.schemas.history import HistoryPage
from app.schemas.transfer import Transfer
from app.services import admin as admin_service
from app.services import history as history_service
from app.services.access import get_event_for_admin

router = APIRouter(prefix="/events/{event_id}/admin", tags=["admin"])


@router.get("/participants")
async def list_participants(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    status: Annotated[list[ApplicationStatus] | None, Query()] = None,
    trip_id: uuid.UUID | None = None,
) -> AdminParticipants:
    event = await get_event_for_admin(session, event_id, user)
    counters = await admin_service.get_counters(session, event.id)
    rows = await admin_service.list_participants(
        session, event.id, statuses=status, trip_id=trip_id
    )
    return AdminParticipants(
        counters=counters,
        participants=[AdminParticipant.build(u, a) for u, a in rows],
    )


@router.get("/transfers")
async def list_transfers(
    event_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[Transfer]:
    event = await get_event_for_admin(session, event_id, user)
    transfers = await admin_service.list_transfers(session, event.id)
    return [Transfer.build(t, viewer_id=None) for t in transfers]


@router.get("/history")
async def get_history(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
) -> HistoryPage:
    event = await get_event_for_admin(session, event_id, user)
    return await history_service.list_history(session, event.id, limit=limit, before_seq=before_seq)
