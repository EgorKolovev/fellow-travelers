"""background jobs

Очередь фоновых заданий в той же БД: задание создаётся в транзакции правки и забирается воркером
через `FOR UPDATE SKIP LOCKED`. Частичный индекс покрывает выборку готовых заданий, частичный
уникальный индекс оставляет по одному ожидающему заданию на группу и вид. Для действующих групп
(`ends_on >= current_date`) сразу ставится задание пересчёта.

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


_INITIAL_RECONCILE = """
INSERT INTO background_jobs (id, event_id, kind, trip_id)
SELECT gen_random_uuid(), e.id, 'reconcile_trip', NULL
  FROM events e
 WHERE e.ends_on >= current_date
"""


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
    # одно ожидающее задание каждого вида на группу: лишние правки сливаются в него
    op.create_index(
        "uq_background_jobs_event_kind_pending",
        "background_jobs",
        ["event_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    # первый пересчёт для действующих групп: до воркера трансферы никто не дополнял и не
    # пересчитывал после правок рейсов. Прошедшие группы и их историю не трогаем
    op.execute(_INITIAL_RECONCILE)


def downgrade() -> None:
    # IF EXISTS: базы, где 0004 накатили до появления этого индекса, откатываются без ошибки
    op.execute("DROP INDEX IF EXISTS uq_background_jobs_event_kind_pending")
    op.drop_index("ix_background_jobs_event_id", table_name="background_jobs")
    op.drop_index(
        "ix_background_jobs_pending_run_after",
        table_name="background_jobs",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("background_jobs")
