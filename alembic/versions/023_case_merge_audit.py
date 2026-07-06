"""add case_merge_audit table (ADR-008 Slice 2, additive schema-only)

Revision ID: 023
Revises: 022
Create Date: 2026-07-06

Additive-only: creates the ``case_merge_audit`` table that records every
loser->winner Case merge performed by migration 024's data migration
(``app.services.case_merge.run_case_merge``). No data migration or backfill
here — this table starts empty. Separating the schema change (023) from the
data migration (024) keeps rollback isolation clean: 023 is a trivially safe
reversible create_table; 024 is the live-data merge that must be run
manually against QA per the runbook.

Reversible create_table, style matches 021/022.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_merge_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        # NOT a ForeignKey: the loser Case row is deleted by migration 024
        # after this row is written — a FK here would either block that
        # deletion or erase the id we need to keep for the audit trail.
        sa.Column("loser_case_id", sa.Integer(), nullable=False),
        sa.Column("winner_case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("loser_lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("rol", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_case_merge_audit_id", "case_merge_audit", ["id"])
    op.create_index("ix_case_merge_audit_loser_case_id", "case_merge_audit", ["loser_case_id"])
    op.create_index("ix_case_merge_audit_winner_case_id", "case_merge_audit", ["winner_case_id"])
    op.create_index("ix_case_merge_audit_rol", "case_merge_audit", ["rol"])


def downgrade() -> None:
    op.drop_index("ix_case_merge_audit_rol", table_name="case_merge_audit")
    op.drop_index("ix_case_merge_audit_winner_case_id", table_name="case_merge_audit")
    op.drop_index("ix_case_merge_audit_loser_case_id", table_name="case_merge_audit")
    op.drop_index("ix_case_merge_audit_id", table_name="case_merge_audit")
    op.drop_table("case_merge_audit")
