import uuid

from fastapi import APIRouter, Response

from app.api.deps import CurrentUser, SessionDep
from app.models import ActorKind
from app.schemas.trip import Trip, TripInput, TripUpdate
from app.services import trips as trips_service
from app.services.access import get_event_for_admin, get_event_for_member
from app.services.audit import Actor

router = APIRouter(tags=["trips"])


@router.get("/events/{event_id}/trips")
async def list_event_trips(
    event_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[Trip]:
    event = await get_event_for_member(session, event_id, user)
    trips = await trips_service.list_event_trips(session, event.id)
    return [Trip.build(trip) for trip in trips]


@router.post("/events/{event_id}/trips", status_code=201)
async def create_trip(
    event_id: uuid.UUID, data: TripInput, user: CurrentUser, session: SessionDep
) -> Trip:
    event = await get_event_for_admin(session, event_id, user)
    return Trip.build(await trips_service.create_trip(session, event, data))


@router.patch("/trips/{trip_id}")
async def update_trip(
    trip_id: uuid.UUID, data: TripUpdate, user: CurrentUser, session: SessionDep
) -> Trip:
    trip = await trips_service.get_trip_for_admin(session, trip_id, user)
    actor = Actor(ActorKind.ADMIN, user.id)
    return Trip.build(await trips_service.update_trip(session, trip, data, actor))


@router.delete("/trips/{trip_id}", status_code=204)
async def delete_trip(trip_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Response:
    trip = await trips_service.get_trip_for_admin(session, trip_id, user)
    await trips_service.delete_trip(session, trip)
    return Response(status_code=204)
