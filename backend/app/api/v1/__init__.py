from fastapi import APIRouter

from app.api.v1 import admin, applications, events, flights, invites, locations, me, my_trip, trips

router = APIRouter()
router.include_router(me.router)
router.include_router(events.router)
router.include_router(invites.router)
router.include_router(locations.router)
router.include_router(trips.router)
router.include_router(flights.router)
router.include_router(applications.router)
router.include_router(my_trip.router)
router.include_router(admin.router)
