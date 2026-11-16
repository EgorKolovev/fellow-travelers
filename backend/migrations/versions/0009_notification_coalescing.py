"""notification coalescing

Защита от потока сообщений (M-04a): статус `sending` (строка арендована отправителем, повтор после
ошибки тоже живёт в нём), вид `several_changes`, частичный уникальный индекс «одна `pending` на
человека в группе» и индекс для подсчёта часового лимита. Перед индексом уже накопленные дубли
`pending` склеиваются: остаётся самая ранняя строка, остальные становятся `skipped`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17 20:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = "ck_notifications_notification_status"
_KIND_CHECK = "ck_notifications_notification_kind"
_STATUSES = ("pending", "sent", "skipped", "failed")
_KINDS = (
    "transfer_assigned",
    "transfer_changed",
    "transfer_left",
    "points_changed",
    "flight_changed",
    "flight_cancelled",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _replace_checks(statuses: tuple[str, ...], kinds: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(_STATUS_CHECK), "notifications", type_="check")
    op.drop_constraint(op.f(_KIND_CHECK), "notifications", type_="check")
    op.create_check_constraint(op.f(_STATUS_CHECK), "notifications", _in("status", statuses))
    op.create_check_constraint(op.f(_KIND_CHECK), "notifications", _in("kind", kinds))


def upgrade() -> None:
    _replace_checks((*_STATUSES, "sending"), (*_KINDS, "several_changes"))
    # дубли `pending` одного человека в группе: остаётся самая ранняя строка
    op.execute(
        """
        UPDATE notifications SET status = 'skipped', last_error = 'Coalesced'
        WHERE status = 'pending' AND id NOT IN (
            SELECT min(id) FROM notifications WHERE status = 'pending' GROUP BY user_id, event_id
        )
        """
    )
    op.create_index(
        "uq_notifications_user_event_pending",
        "notifications",
        ["user_id", "event_id"],
        unique=True,
        postgresql_where="status = 'pending'",
    )
    op.create_index(
        "ix_notifications_user_event_created",
        "notifications",
        ["user_id", "event_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_event_created", table_name="notifications")
    op.drop_index("uq_notifications_user_event_pending", table_name="notifications")
    # строка в отправке без воркера вернётся в очередь; склейка видов в прежний набор
    op.execute("UPDATE notifications SET status = 'pending' WHERE status = 'sending'")
    op.execute("UPDATE notifications SET kind = 'transfer_changed' WHERE kind = 'several_changes'")
    _replace_checks(_STATUSES, _KINDS)
