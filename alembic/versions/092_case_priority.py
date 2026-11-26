"""case_priority: per-lawyer priority marks on causas

Revision ID: 092
Revises: 091
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "092"
down_revision: Union[str, None] = "091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_priority",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "lawyer_id", name="uq_case_priority_case_lawyer"),
    )
    op.create_index("ix_case_priority_lawyer_id", "case_priority", ["lawyer_id"])


def downgrade() -> None:
    op.drop_index("ix_case_priority_lawyer_id", table_name="case_priority")
    op.drop_table("case_priority")
