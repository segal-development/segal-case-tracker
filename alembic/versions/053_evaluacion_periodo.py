"""add periodo (YYYY-MM) to evaluaciones for the monthly limit

Adds a ``periodo`` column ("YYYY-MM") to ``evaluaciones`` so each evaluation is
bucketed by month. The monthly limit (1 evaluation per evaluador+evaluado+mes)
is enforced in the endpoint — mirroring how hitos dedup works — so NO DB unique
constraint is added here (existing data may contain duplicates).

Column is added nullable, backfilled from ``created_at``, then made NOT NULL.

Revision ID: 053
Revises: 052
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "053"
down_revision: Union[str, None] = "052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Add nullable so existing rows can be backfilled first.
    op.add_column(
        "evaluaciones",
        sa.Column("periodo", sa.String(length=7), nullable=True),
    )
    # 2) Backfill existing rows from their created_at month.
    op.execute(
        "UPDATE evaluaciones SET periodo = to_char(created_at, 'YYYY-MM') "
        "WHERE periodo IS NULL"
    )
    # 3) Now enforce NOT NULL.
    op.alter_column("evaluaciones", "periodo", nullable=False)
    # 4) Index the month bucket (backs the monthly-limit lookup and filters).
    op.create_index("ix_evaluaciones_periodo", "evaluaciones", ["periodo"])


def downgrade() -> None:
    op.drop_index("ix_evaluaciones_periodo", table_name="evaluaciones")
    op.drop_column("evaluaciones", "periodo")
