"""background jobs

Очередь фоновых заданий в той же БД: задание создаётся в транзакции правки и забирается воркером
через `FOR UPDATE SKIP LOCKED`. Частичный индекс покрывает выборку готовых заданий.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "reconcile_trip",
                name="job_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("trip_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "failed",
                name="job_status",
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
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_background_jobs_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_background_jobs")),
    )
    op.create_index(
        "ix_background_jobs_pending_run_after",
        "background_jobs",
        ["run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_background_jobs_event_id", "background_jobs", ["event_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_background_jobs_event_id", table_name="background_jobs")
    op.drop_index(
        "ix_background_jobs_pending_run_after",
        table_name="background_jobs",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("background_jobs")
