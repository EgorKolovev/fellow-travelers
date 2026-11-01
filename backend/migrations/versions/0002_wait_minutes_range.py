"""wait minutes range 0-1440

Время ожидания в заявке — любое целое от 0 до 1440 минут вместо фиксированного набора 0/15/30.

Откат с потерей точности: перед возвратом старого CHECK значения приводятся к прежнему
набору (1–15 → 15, больше 15 → 30), исходные значения не восстанавливаются.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01 12:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_applications_max_wait_minutes_allowed"), "applications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_applications_max_wait_minutes_range"),
        "applications",
        "max_wait_minutes BETWEEN 0 AND 1440",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE applications SET max_wait_minutes = CASE"
        " WHEN max_wait_minutes BETWEEN 1 AND 15 THEN 15"
        " WHEN max_wait_minutes > 15 THEN 30"
        " ELSE max_wait_minutes END"
        " WHERE max_wait_minutes NOT IN (0, 15, 30)"
    )
    op.drop_constraint(
        op.f("ck_applications_max_wait_minutes_range"), "applications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_applications_max_wait_minutes_allowed"),
        "applications",
        "max_wait_minutes IN (0, 15, 30)",
    )
