"""departed from transfer

`applications.departed_from_transfer_id`: трансфер, из которого заявка уехала (Д7). Без внешнего
ключа: при расформировании трансфер удаляется, а поле нужно при отмене «Уехал», чтобы отличить
«трансфер ещё есть» от «трансфера уже нет». У прежних заявок пусто.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 16:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("departed_from_transfer_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "departed_from_transfer_id")
