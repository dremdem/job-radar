"""opportunities.not_accepting_at (#278)

Revision ID: 6a1d3e8f2c74
Revises: 4f7a2c9e1b63
Create Date: 2026-11-08 15:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a1d3e8f2c74"
down_revision: str | None = "4f7a2c9e1b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("not_accepting_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.drop_column("not_accepting_at")
