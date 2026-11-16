import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Application as ApplicationModel
from app.models import Transfer as TransferModel
from app.models import TransferStatus
from app.schemas.user import Contact
from app.timezones import to_local


class TransferMembersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_ids: list[uuid.UUID] = Field(max_length=10)


class TransferMember(BaseModel):
    application_id: uuid.UUID
    is_me: bool
    contact: Contact
    passengers: Literal[1, 2]
    baggage_count: int
    trip_number: str
    effective_arrival: datetime
    at_meeting_point: bool
    departed_at: datetime | None

    @classmethod
    def build(
        cls, application: ApplicationModel, *, viewer_id: uuid.UUID | None
    ) -> "TransferMember":
        """Пользователь, рейс и локация прилёта заявки должны быть загружены."""
        user = application.user
        trip = application.trip
        departed_at = application.departed_at
        return cls(
            application_id=application.id,
            is_me=viewer_id is not None and application.user_id == viewer_id,
            contact=Contact.from_user(user),
            passengers=2 if application.with_companion else 1,
            baggage_count=application.baggage_count,
            trip_number=trip.number,
            effective_arrival=to_local(trip.effective_arrival, trip.arrival_location.timezone),
            at_meeting_point=application.at_meeting_point,
            departed_at=None if departed_at is None else departed_at.astimezone(UTC),
        )


class Transfer(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    status: TransferStatus
    passengers_total: int
    baggage_total: int
    members: list[TransferMember]
    created_at: datetime

    @classmethod
    def build(cls, transfer: TransferModel, *, viewer_id: uuid.UUID | None = None) -> "Transfer":
        """Участники (`applications`) с пользователем, рейсом и локацией прилёта загружены.

        `viewer_id` — чьими глазами смотрим (для `is_me`); None — ни один участник не «я».
        """
        ordered = sorted(
            transfer.applications,
            key=lambda a: (
                a.trip.effective_arrival,
                a.created_at,
                a.id,
            ),
        )
        members = [TransferMember.build(a, viewer_id=viewer_id) for a in ordered]
        return cls(
            id=transfer.id,
            event_id=transfer.event_id,
            status=transfer.status,
            passengers_total=sum(m.passengers for m in members),
            baggage_total=sum(m.baggage_count for m in members),
            members=members,
            created_at=transfer.created_at.astimezone(UTC),
        )
