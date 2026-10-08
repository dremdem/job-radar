"""company events

Revision ID: 5d1e7a2c9b30
Revises: 9cf47be10ecd
Create Date: 2026-10-08 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d1e7a2c9b30"
down_revision: str | None = "9cf47be10ecd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("company_events", schema=None) as batch_op:
        batch_op.create_index("ix_company_events_company_id", ["company_id"], unique=False)
        batch_op.create_index("ix_company_events_opportunity_id", ["opportunity_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("company_events", schema=None) as batch_op:
        batch_op.drop_index("ix_company_events_opportunity_id")
        batch_op.drop_index("ix_company_events_company_id")
    op.drop_table("company_events")
