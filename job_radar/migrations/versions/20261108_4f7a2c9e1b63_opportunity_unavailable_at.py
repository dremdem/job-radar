"""opportunities.unavailable_at, from the "unavailable" marks in the history (#276)

Revision ID: 4f7a2c9e1b63
Revises: 8c3f5b2d9e61
Create Date: 2026-11-08 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f7a2c9e1b63"
down_revision: str | None = "8c3f5b2d9e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unavailable_at", sa.DateTime(), nullable=True))
    marked = (
        "SELECT MIN(e.at) FROM company_events e"
        " WHERE e.kind = 'unavailable' AND e.opportunity_id = opportunities.id"
    )
    op.execute(f"UPDATE opportunities SET unavailable_at = ({marked})")
    # The new auto-ignore rule is on by default, and nobody can have switched it off yet.
    op.execute(
        "UPDATE opportunities SET state = 'auto_ignore',"
        " auto_ignore_reason = 'vacancy not available'"
        " WHERE state = 'new' AND unavailable_at IS NOT NULL"
    )
    op.execute("DELETE FROM company_events WHERE kind = 'unavailable'")


def downgrade() -> None:
    op.execute(
        "INSERT INTO company_events (company_id, opportunity_id, kind, at, comment, created_at)"
        " SELECT company_id, id, 'unavailable', unavailable_at,"
        " title || ': vacancy not available', unavailable_at"
        " FROM opportunities WHERE unavailable_at IS NOT NULL"
    )
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.drop_column("unavailable_at")
