"""renovaciones: make lawyer_id nullable + add renovador_raw (for Excel import)

Imported historical renovaciones may be done by cobranza/sales staff who aren't
system lawyers; those keep only ``renovador_raw`` (original name). Manual entries
still set lawyer_id.

Revision ID: 037
Revises: 036
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("renovaciones", sa.Column("renovador_raw", sa.String(length=100), nullable=True))
    op.alter_column("renovaciones", "lawyer_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.alter_column("renovaciones", "lawyer_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("renovaciones", "renovador_raw")
