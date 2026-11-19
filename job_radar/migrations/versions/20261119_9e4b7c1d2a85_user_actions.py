"""user_actions: decisions in the queue, for undo (#56)

Revision ID: 9e4b7c1d2a85
Revises: 6a1d3e8f2c74
Create Date: 2026-11-19 10:30:00
"""

import collections.abc as cabc

import alembic.op as op
import sqlalchemy as sa

revision: str = "9e4b7c1d2a85"
down_revision: str | None = "6a1d3e8f2c74"
branch_labels: str | cabc.Sequence[str] | None = None
depends_on: str | cabc.Sequence[str] | None = None

STATES = ["new", "interested", "ignore", "applied", "auto_ignore"]


def upgrade() -> None:
    op.create_table(
        "user_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column(
            "from_state",
            sa.Enum(*STATES, name="opportunitystate", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "to_state",
            sa.Enum(*STATES, name="opportunitystate", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("from_state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_auto_ignore_reason", sa.String(length=255), nullable=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_actions_opportunity_id", "user_actions", ["opportunity_id"])


def downgrade() -> None:
    op.drop_index("ix_user_actions_opportunity_id", table_name="user_actions")
    op.drop_table("user_actions")
