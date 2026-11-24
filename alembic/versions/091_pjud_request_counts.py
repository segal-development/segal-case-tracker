"""pjud_request_counts: hourly PJUD request/rejection counters

Revision ID: 091
Revises: 090
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "091"
down_revision: Union[str, None] = "090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pjud_request_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hour_utc", sa.DateTime(), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), nullable=True),
        sa.Column("egress_ip", sa.String(45), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "hour_utc", "lawyer_id", "egress_ip", "action", "kind",
            name="uq_pjud_request_counts_bucket",
        ),
    )
    op.create_index("ix_pjud_request_counts_id", "pjud_request_counts", ["id"])
    op.create_index("ix_pjud_request_counts_hour_utc", "pjud_request_counts", ["hour_utc"])


def downgrade() -> None:
    op.drop_index("ix_pjud_request_counts_hour_utc", table_name="pjud_request_counts")
    op.drop_index("ix_pjud_request_counts_id", table_name="pjud_request_counts")
    op.drop_table("pjud_request_counts")
