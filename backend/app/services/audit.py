import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActorKind, Application, AuditAction, AuditLog, AuditReason
from app.observability import context


@dataclass(frozen=True, slots=True)
class Actor:
    """Кто сделал изменение: участник или админ (с id пользователя) либо сервис без пользователя."""

    kind: ActorKind
    user_id: uuid.UUID | None


SYSTEM = Actor(ActorKind.SYSTEM, None)

# длина колонки audit_log.request_id; чужой заголовок не должен ронять сценарий
REQUEST_ID_MAX_LENGTH = 64


def iso(moment: datetime) -> str:
    """Момент времени для `details`: ISO 8601 в UTC со смещением, без зависимости от таймзоны."""
    return moment.astimezone(UTC).isoformat()


def record(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    action: AuditAction,
    actor: Actor,
    reason: AuditReason | None = None,
    application: Application | None = None,
    transfer_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Добавляет запись истории в текущую транзакцию: только `session.add`, без flush и commit.

    `subject_user_id` и `application_id` берутся из заявки, `request_id` и `job_id` — из контекста
    операции. Запись живёт и умирает вместе со сценарием: при откате исчезает вместе с изменением.

    `details` — только поля из таблицы «История» в `docs/contracts/README.md`: статусы значениями
    enum, моменты через `iso()`, id строками. Телефона, имён и username там быть не должно.
    """
    request_id = context.request_id.get()
    session.add(
        AuditLog(
            event_id=event_id,
            action=action,
            reason=reason,
            actor_kind=actor.kind,
            actor_user_id=actor.user_id,
            subject_user_id=application.user_id if application is not None else None,
            application_id=application.id if application is not None else None,
            transfer_id=transfer_id,
            trip_id=trip_id,
            job_id=context.job_id.get(),
            request_id=request_id[:REQUEST_ID_MAX_LENGTH] if request_id is not None else None,
            details=dict(details) if details is not None else {},
        )
    )
