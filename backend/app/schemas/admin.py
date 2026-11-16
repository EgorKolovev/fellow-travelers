import uuid

from pydantic import BaseModel

from app.models import Application as ApplicationModel
from app.models import User
from app.schemas.application import Application
from app.schemas.trip import Trip
from app.schemas.user import Contact


class Counters(BaseModel):
    expected: int
    searching: int
    assigned: int
    solo: int
    at_meeting_point: int
    departed: int


class AdminParticipant(BaseModel):
    contact: Contact
    application: Application | None

    @classmethod
    def build(cls, user: User, application: ApplicationModel | None) -> "AdminParticipant":
        """Рейс заявки вместе с его локациями должен быть загружен."""
        return cls(
            contact=Contact.from_user(user),
            application=None if application is None else Application.build(application),
        )


class AdminParticipants(BaseModel):
    counters: Counters
    participants: list[AdminParticipant]


class BoardTrip(BaseModel):
    trip: Trip
    people: int
    application_ids: list[uuid.UUID]


class Board(BaseModel):
    counters: Counters
    trips: list[BoardTrip]
