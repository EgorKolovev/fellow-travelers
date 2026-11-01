import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, str_enum
from app.models.enums import ApplicationStatus
from app.models.transfer import Transfer
from app.models.trip import Trip
from app.models.user import User


class Application(TimestampMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_applications_event_user"),
        CheckConstraint("baggage_count BETWEEN 0 AND 2", name="baggage_count_range"),
        CheckConstraint("max_wait_minutes BETWEEN 0 AND 1440", name="max_wait_minutes_range"),
        CheckConstraint(
            "(status = 'assigned') = (transfer_id IS NOT NULL)", name="assigned_has_transfer"
        ),
        Index("ix_applications_event_id_status", "event_id", "status"),
        Index("ix_applications_transfer_id", "transfer_id"),
        Index("ix_applications_trip_id", "trip_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # trip_id и transfer_id намеренно без ondelete (NO ACTION): при удалении события каскадом
    # уходят и trips/transfers, и applications; NO ACTION проверяется в конце оператора, а
    # RESTRICT проверяется сразу и сломал бы каскад.
    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id"))
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfers.id"))
    with_companion: Mapped[bool]
    baggage_count: Mapped[int] = mapped_column(SmallInteger)
    max_wait_minutes: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[ApplicationStatus] = mapped_column(
        str_enum(ApplicationStatus, name="application_status"),
        server_default=ApplicationStatus.SEARCHING.value,
    )
    at_meeting_point: Mapped[bool] = mapped_column(server_default=false())
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trip: Mapped[Trip] = relationship(lazy="raise")
    user: Mapped[User] = relationship(lazy="raise")
    transfer: Mapped[Transfer | None] = relationship(back_populates="applications", lazy="raise")
