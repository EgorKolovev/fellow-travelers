"""notifications

Таблица уведомлений (M-04, T9): сообщение пишется в транзакции изменения, воркер (T10) отправляет.
Уникальный индекс `(user_id, dedupe_key)` не даёт создать одно сообщение дважды; индекс
`(status, run_after)` покрывает выборку готовых к отправке.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "transfer_assigned",
                "transfer_changed",
                "transfer_left",
                "points_changed",
                "flight_changed",
                "flight_cancelled",
                name="notification_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "skipped",
                "failed",
                name="notification_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_notifications_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        "uq_notifications_user_id_dedupe_key",
        "notifications",
        ["user_id", "dedupe_key"],
        unique=True,
    )
    op.create_index("ix_notifications_status_run_after", "notifications", ["status", "run_after"])
    op.create_index("ix_notifications_event_id", "notifications", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_event_id", table_name="notifications")
    op.drop_index("ix_notifications_status_run_after", table_name="notifications")
    op.drop_index("uq_notifications_user_id_dedupe_key", table_name="notifications")
    op.drop_table("notifications")
