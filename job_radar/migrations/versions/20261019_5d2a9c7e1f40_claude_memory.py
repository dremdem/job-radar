"""claude_memory, claude_memory_changes and claude_messages.memory_note

Revision ID: 5d2a9c7e1f40
Revises: 3b8e6f1a4c27
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d2a9c7e1f40"
down_revision: str | None = "3b8e6f1a4c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claude_memory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "claude_memory_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("op", sa.String(length=16), nullable=False),
        sa.Column("by", sa.String(length=16), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claude_memory_changes_at", "claude_memory_changes", ["at"], unique=False)
    with op.batch_alter_table("claude_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("memory_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("claude_messages", schema=None) as batch_op:
        batch_op.drop_column("memory_note")
    op.drop_index("ix_claude_memory_changes_at", table_name="claude_memory_changes")
    op.drop_table("claude_memory_changes")
    op.drop_table("claude_memory")
