"""ats_boards and ats_lookups: original postings on employer ATS boards (#2)

Revision ID: b7d3e2a91c40
Revises: 9e4b7c1d2a85
Create Date: 2026-11-19 12:00:00
"""

import collections.abc as cabc

import alembic.op as op
import sqlalchemy as sa

revision: str = "b7d3e2a91c40"
down_revision: str | None = "9e4b7c1d2a85"
branch_labels: str | cabc.Sequence[str] | None = None
depends_on: str | cabc.Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ats_boards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("found_by", sa.String(length=16), nullable=False),
        sa.Column("board_name", sa.String(length=255), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "slug"),
    )
    op.create_index("ix_ats_boards_company_id", "ats_boards", ["company_id"])
    op.create_table(
        "ats_lookups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opportunity_id"),
    )


def downgrade() -> None:
    op.drop_table("ats_lookups")
    op.drop_index("ix_ats_boards_company_id", table_name="ats_boards")
    op.drop_table("ats_boards")
