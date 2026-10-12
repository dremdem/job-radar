"""opportunity state_changed_at

Revision ID: 7a4c1e9d2b55
Revises: 5d1e7a2c9b30
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a4c1e9d2b55"
down_revision: str | None = "5d1e7a2c9b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("state_changed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.drop_column("state_changed_at")
