import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, SessionDep, get_flight_provider
from app.flights import FlightProvider
from app.schemas.trip import Trip
from app.services import flights as flights_service

router = APIRouter(tags=["trips"])

ProviderDep = Annotated[FlightProvider | None, Depends(get_flight_provider)]


@router.get("/flights/search")
async def search_flights(
    event_id: Annotated[uuid.UUID, Query()],
    number: Annotated[str, Query(pattern=r"^[A-Za-z0-9]{2,3}\s?\d{1,5}$")],
    departure_date: Annotated[date, Query(alias="date")],
    user: CurrentUser,
    session: SessionDep,
    provider: ProviderDep,
) -> list[Trip]:
    trips = await flights_service.search_flights(
        session, event_id, user, number, departure_date, provider
    )
    return [Trip.build(trip) for trip in trips]
