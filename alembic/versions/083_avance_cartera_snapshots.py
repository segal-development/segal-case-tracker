"""avance_cartera_snapshots

Revision ID: 083
Revises: 082
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "083"
down_revision: Union[str, None] = "082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "avance_cartera_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("periodo", sa.String(7), nullable=False),
        sa.Column("desde", sa.Date(), nullable=False),
        sa.Column("hasta", sa.Date(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("taken_by", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
    )
    op.create_index("ix_avance_cartera_snapshots_id", "avance_cartera_snapshots", ["id"])
    op.create_index(
        "ix_avance_cartera_snapshots_periodo", "avance_cartera_snapshots", ["periodo"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_avance_cartera_snapshots_periodo", table_name="avance_cartera_snapshots")
    op.drop_index("ix_avance_cartera_snapshots_id", table_name="avance_cartera_snapshots")
    op.drop_table("avance_cartera_snapshots")
