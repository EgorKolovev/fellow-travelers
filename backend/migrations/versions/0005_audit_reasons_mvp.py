"""audit reasons mvp

Четыре новые причины истории: application_changed, departure_undone, flight_cancelled,
provider_update. Колонка audit_log.reason хранится как строка с CHECK-ограничением, поэтому
ограничение пересоздаётся с расширенным списком значений.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 12:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_audit_log_audit_reason"
_OLD_REASONS = (
    "go_solo",
    "cancel",
    "resume_search",
    "auto_match",
    "auto_fill",
    "incompatible_after_change",
    "member_left",
    "all_departed",
    "admin_edit",
)
_NEW_REASONS = (
    "application_changed",
    "departure_undone",
    "flight_cancelled",
    "provider_update",
)


def _condition(reasons: tuple[str, ...]) -> str:
    return "reason IN (" + ", ".join(f"'{reason}'" for reason in reasons) + ")"


def _replace_constraint(reasons: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), "audit_log", _condition(reasons))


def upgrade() -> None:
    _replace_constraint(_OLD_REASONS + _NEW_REASONS)


def downgrade() -> None:
    # audit_log только для добавления: строки удалить нельзя, и если причины 0005 уже записаны,
    # Postgres откажет в создании ограничения
    _replace_constraint(_OLD_REASONS)
