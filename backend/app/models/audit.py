import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import ActorKind, AuditAction, AuditReason


class AuditLog(Base):
    """История изменений группы: только добавление, UPDATE и DELETE запрещены триггером БД.

    `id` растёт монотонно и задаёт порядок записей (`seq` в API). `application_id`, `transfer_id`
    и `trip_id` без FK: трансфер удаляется при расформировании, а запись о нём остаётся.
    `event_id` с CASCADE уводит историю вместе с группой; при удалении профиля пользователь
    в записях заменяется на NULL. Телефона, имён и username в записях нет.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_event_id_id", "event_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    action: Mapped[AuditAction] = mapped_column(
        str_enum(AuditAction, name="audit_action", length=32)
    )
    reason: Mapped[AuditReason | None] = mapped_column(
        str_enum(AuditReason, name="audit_reason", length=32)
    )
    actor_kind: Mapped[ActorKind] = mapped_column(str_enum(ActorKind, name="actor_kind", length=32))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    application_id: Mapped[uuid.UUID | None]
    transfer_id: Mapped[uuid.UUID | None]
    trip_id: Mapped[uuid.UUID | None]
    job_id: Mapped[uuid.UUID | None]
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
