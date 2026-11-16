"""email_messages.hidden_at

Revision ID: 8c3f5b2d9e61
Revises: 5d2a9c7e1f40
Create Date: 2026-10-24 12:00:00
"""

import collections.abc as cabc

import alembic.op as op
import sqlalchemy as sa

revision: str = "8c3f5b2d9e61"
down_revision: str | None = "5d2a9c7e1f40"
branch_labels: str | cabc.Sequence[str] | None = None
depends_on: str | cabc.Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("hidden_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("email_messages", schema=None) as batch_op:
        batch_op.drop_column("hidden_at")
