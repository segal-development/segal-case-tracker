"""case_assignment_history: previous owner of every assignment write

Revision ID: 078
Revises: 077

One row per case per batch (traspaso de cartera or manual reasignar), holding the previous
override columns exactly as stored so the batch can be reverted. Additive: a new table.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "078"
down_revision: Union[str, None] = "077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_assignment_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("from_lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
        sa.Column("prev_assigned_lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
        sa.Column("prev_assigned_at", sa.DateTime(), nullable=True),
        sa.Column("prev_assigned_by_rut", sa.String(20), nullable=True),
        sa.Column("prev_assigned_motivo", sa.String(255), nullable=True),
        sa.Column("to_lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("motivo", sa.String(255), nullable=True),
        sa.Column("created_by_rut", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_case_assignment_history_batch_id", "case_assignment_history", ["batch_id"])
    op.create_index("ix_case_assignment_history_case_id", "case_assignment_history", ["case_id"])
    op.create_index("ix_case_assignment_history_id", "case_assignment_history", ["id"])


def downgrade() -> None:
    op.drop_index("ix_case_assignment_history_id", table_name="case_assignment_history")
    op.drop_index("ix_case_assignment_history_case_id", table_name="case_assignment_history")
    op.drop_index("ix_case_assignment_history_batch_id", table_name="case_assignment_history")
    op.drop_table("case_assignment_history")
