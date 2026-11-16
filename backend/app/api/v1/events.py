import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas.event import Event, EventCreate, EventUpdate, Invite
from app.services import events as events_service
from app.services.access import get_event_for_admin, get_event_for_member, is_member

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", status_code=201)
async def create_event(data: EventCreate, user: CurrentUser, session: SessionDep) -> Event:
    event = await events_service.create_event(session, user, data)
    return Event.build(event, user_id=user.id, is_participant=False)


@router.get("/{event_id}")
async def get_event(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Event:
    event = await get_event_for_member(session, event_id, user)
    return Event.build(
        event, user_id=user.id, is_participant=await is_member(session, event.id, user.id)
    )


@router.patch("/{event_id}")
async def update_event(
    event_id: uuid.UUID, data: EventUpdate, user: CurrentUser, session: SessionDep
) -> Event:
    event = await get_event_for_admin(session, event_id, user)
    event = await events_service.update_event(session, event, data, user)
    return Event.build(
        event, user_id=user.id, is_participant=await is_member(session, event.id, user.id)
    )


@router.get("/{event_id}/invite")
async def get_event_invite(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Invite:
    event = await get_event_for_admin(session, event_id, user)
    return Invite(code=event.invite_code, link=events_service.build_invite_link(event.invite_code))
