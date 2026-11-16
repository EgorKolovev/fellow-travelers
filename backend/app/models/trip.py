import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, str_enum
from app.models.enums import TransportMode, TripSource, TripStatus
from app.models.location import Location


class Trip(TimestampMixin, Base):
    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint("scheduled_arrival > scheduled_departure", name="arrival_after_departure"),
        UniqueConstraint(
            "event_id",
            "mode",
            "number",
            "scheduled_departure",
            name="uq_trips_event_mode_number_departure",
        ),
        Index("ix_trips_event_id_scheduled_arrival", "event_id", "scheduled_arrival"),
        # один рейс поставщика на группу; у ручных рейсов provider_ref пуст и в индекс не входит
        Index(
            "uq_trips_event_id_provider_ref",
            "event_id",
            "provider_ref",
            unique=True,
            postgresql_where=text("provider_ref IS NOT NULL"),
        ),
        Index("ix_trips_next_poll_at", "next_poll_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    mode: Mapped[TransportMode] = mapped_column(str_enum(TransportMode, name="transport_mode"))
    number: Mapped[str] = mapped_column(String(16))
    departure_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )
    arrival_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )
    scheduled_departure: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estimated_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[TripStatus] = mapped_column(
        str_enum(TripStatus, name="trip_status"), server_default=TripStatus.SCHEDULED.value
    )
    source: Mapped[TripSource] = mapped_column(
        str_enum(TripSource, name="trip_source"), server_default=TripSource.MANUAL.value
    )
    provider_ref: Mapped[str | None] = mapped_column(String(128))
    # опрос поставщика (воркер, T8): когда опрашивать, когда опрашивали, о каком прибытии сообщали
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    departure_location: Mapped[Location] = relationship(
        foreign_keys=[departure_location_id], lazy="raise"
    )
    arrival_location: Mapped[Location] = relationship(
        foreign_keys=[arrival_location_id], lazy="raise"
    )

    @hybrid_property
    def effective_arrival(self) -> datetime:
        """Расчётное прибытие, если известно, иначе по расписанию — по нему идёт подбор."""
        return self.estimated_arrival or self.scheduled_arrival

    @effective_arrival.inplace.expression
    @classmethod
    def _effective_arrival_expression(cls) -> ColumnElement[datetime]:
        return func.coalesce(cls.estimated_arrival, cls.scheduled_arrival)
