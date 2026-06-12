"""Add last_detail_checked_at to cases table.

Revision ID: 007
Revises: 006
Create Date: 2026-06-12

Slice 1 (sync-rotacion): Tracks when a case was last detail-scraped so the
rotation scheduler can advance through the full case list fairly.

Changes:
- cases.last_detail_checked_at  TIMESTAMP  nullable
- ix_cases_last_detail_checked_at  btree index (supports ORDER BY in rotation query)

Rollback: alembic downgrade -1 drops index then column (no data loss — column
is nullable, NULL = never-checked cases, which are highest rotation priority).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("last_detail_checked_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_cases_last_detail_checked_at",
        "cases",
        ["last_detail_checked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cases_last_detail_checked_at", table_name="cases")
    op.drop_column("cases", "last_detail_checked_at")
