"""trip polling

Поля опроса поставщика рейсов в `trips`: `next_poll_at`, `last_polled_at`, `notified_arrival`.
Уникальный частичный индекс `(event_id, provider_ref)` не даёт сохранить один рейс поставщика
в группе дважды (у ручных рейсов `provider_ref` пуст). Индекс по `next_poll_at` для выборки
рейсов, которые пора опросить.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trips", sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trips", sa.Column("notified_arrival", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_trips_event_id_provider_ref",
        "trips",
        ["event_id", "provider_ref"],
        unique=True,
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )
    op.create_index("ix_trips_next_poll_at", "trips", ["next_poll_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trips_next_poll_at", table_name="trips")
    op.drop_index(
        "uq_trips_event_id_provider_ref",
        table_name="trips",
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )
    op.drop_column("trips", "notified_arrival")
    op.drop_column("trips", "last_polled_at")
    op.drop_column("trips", "next_poll_at")
