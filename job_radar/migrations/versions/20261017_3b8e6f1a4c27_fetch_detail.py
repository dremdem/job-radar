"""linkedin_fetches detail and opportunity_id

Revision ID: 3b8e6f1a4c27
Revises: 7a4c1e9d2b55
Create Date: 2026-10-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b8e6f1a4c27"
down_revision: str | None = "7a4c1e9d2b55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("linkedin_fetches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("detail", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("opportunity_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_linkedin_fetches_opportunity_id"), ["opportunity_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("linkedin_fetches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_linkedin_fetches_opportunity_id"))
        batch_op.drop_column("opportunity_id")
        batch_op.drop_column("detail")
