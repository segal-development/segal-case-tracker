"""add case_lawyer_source table (ADR-002, additive schema-only)

Revision ID: 022
Revises: 021
Create Date: 2026-07-06

Additive-only: creates the ``case_lawyer_source`` M:N association table that
records which lawyer(s) have seen a given Case in their live PJUD "Mis
Causas" list. No data migration or backfill here (that is PR2's
merge-duplicate-ROLs migration, 023) — this table starts empty and is
populated going forward by ``IngestService.ingest_cases``/``ingest_movements``.

Reversible create_table, style matches 021.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_lawyer_source",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("case_id", "lawyer_id", name="uq_case_lawyer_source_case_lawyer"),
    )
    op.create_index(
        "ix_case_lawyer_source_id", "case_lawyer_source", ["id"]
    )
    op.create_index(
        "ix_case_lawyer_source_case_id", "case_lawyer_source", ["case_id"]
    )
    op.create_index(
        "ix_case_lawyer_source_lawyer_id", "case_lawyer_source", ["lawyer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_case_lawyer_source_lawyer_id", table_name="case_lawyer_source")
    op.drop_index("ix_case_lawyer_source_case_id", table_name="case_lawyer_source")
    op.drop_index("ix_case_lawyer_source_id", table_name="case_lawyer_source")
    op.drop_table("case_lawyer_source")
