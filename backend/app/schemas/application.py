import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Application as ApplicationModel
from app.models import ApplicationStatus
from app.schemas.trip import Trip

# strict: строки, bool и float (даже 15.0) отклоняются — контракт требует integer
WaitMinutes = Annotated[int, Field(strict=True, ge=0, le=1440)]


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: uuid.UUID
    with_companion: bool
    baggage_count: Annotated[int, Field(ge=0, le=2)]
    max_wait_minutes: WaitMinutes
    seek_companions: bool | None = None

    @field_validator("seek_companions", mode="before")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        # необязательное поле: можно не передавать, но null контракт не допускает
        if value is None:
            raise ValueError("Поле не может быть null")
        return value


class MarksInput(BaseModel):
    """Передаются только меняемые поля; хотя бы одно обязательно."""

    model_config = ConfigDict(extra="forbid")

    at_meeting_point: bool | None = None
    departed: bool | None = None

    @field_validator("at_meeting_point", "departed", mode="before")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Поле не может быть null")
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> "MarksInput":
        if not self.model_fields_set:
            raise ValueError("Передайте хотя бы одно поле")
        return self


class Application(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    trip: Trip
    with_companion: bool
    passengers: Literal[1, 2]
    baggage_count: int
    max_wait_minutes: WaitMinutes
    status: ApplicationStatus
    at_meeting_point: bool
    departed_at: datetime | None
    transfer_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, application: ApplicationModel) -> "Application":
        """Рейс заявки вместе с его локациями должен быть загружен."""
        departed_at = application.departed_at
        return cls(
            id=application.id,
            event_id=application.event_id,
            trip=Trip.build(application.trip),
            with_companion=application.with_companion,
            passengers=2 if application.with_companion else 1,
            baggage_count=application.baggage_count,
            max_wait_minutes=application.max_wait_minutes,
            status=application.status,
            at_meeting_point=application.at_meeting_point,
            departed_at=None if departed_at is None else departed_at.astimezone(UTC),
            transfer_id=application.transfer_id,
            created_at=application.created_at.astimezone(UTC),
            updated_at=application.updated_at.astimezone(UTC),
        )
