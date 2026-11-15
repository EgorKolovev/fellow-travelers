import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models import ActorKind, AuditAction, AuditLog, AuditReason, User


class HistoryPerson(BaseModel):
    """Только чтобы понять, о ком запись: без телефона и username."""

    user_id: uuid.UUID
    first_name: str
    last_name: str | None

    @classmethod
    def from_user(cls, user: User) -> "HistoryPerson":
        return cls(user_id=user.id, first_name=user.first_name, last_name=user.last_name)


class HistoryEntry(BaseModel):
    seq: int
    occurred_at: datetime
    action: AuditAction
    reason: AuditReason | None
    actor_kind: ActorKind
    actor: HistoryPerson | None
    subject: HistoryPerson | None
    application_id: uuid.UUID | None
    transfer_id: uuid.UUID | None
    trip_id: uuid.UUID | None
    details: dict[str, Any]

    @classmethod
    def build(cls, record: AuditLog, actor: User | None, subject: User | None) -> "HistoryEntry":
        """`actor` и `subject` — пользователи записи; None, если их нет или профиль удалён."""
        return cls(
            seq=record.id,
            occurred_at=record.occurred_at,
            action=record.action,
            reason=record.reason,
            actor_kind=record.actor_kind,
            actor=None if actor is None else HistoryPerson.from_user(actor),
            subject=None if subject is None else HistoryPerson.from_user(subject),
            application_id=record.application_id,
            transfer_id=record.transfer_id,
            trip_id=record.trip_id,
            details=record.details,
        )


class HistoryPage(BaseModel):
    items: list[HistoryEntry]
    next_before_seq: int | None
