import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import NotificationKind, NotificationStatus


class Notification(Base):
    """Сообщение участнику в Telegram: пишется в одной транзакции с изменением, шлёт воркер.

    `payload` хранит только белый список (идентификаторы, статус, время прибытия): телефонов и
    имён в нём нет, текст собирается при отправке. Пара `(user_id, dedupe_key)` уникальна, повтор
    с тем же ключом не создаёт второе сообщение. Воркер берёт `pending` с `run_after <= now()`;
    в `last_error` только имя класса ошибки. Строки уходят вместе с группой и с пользователем.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("uq_notifications_user_id_dedupe_key", "user_id", "dedupe_key", unique=True),
        Index("ix_notifications_status_run_after", "status", "run_after"),
        Index("ix_notifications_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    kind: Mapped[NotificationKind] = mapped_column(
        str_enum(NotificationKind, name="notification_kind", length=32)
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, name="notification_status"),
        server_default=NotificationStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
