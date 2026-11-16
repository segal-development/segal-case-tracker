"""procurador_abogados: the 1-2 firm lawyers each procurador account supports

Revision ID: 086
Revises: 085
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "086"
down_revision: Union[str, None] = "085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "procurador_abogados",
        sa.Column("procurador_id", sa.Integer(), sa.ForeignKey("lawyers.id"), primary_key=True),
        sa.Column("abogado_id", sa.Integer(), sa.ForeignKey("lawyers.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_procurador_abogados_abogado_id", "procurador_abogados", ["abogado_id"])


def downgrade() -> None:
    op.drop_index("ix_procurador_abogados_abogado_id", table_name="procurador_abogados")
    op.drop_table("procurador_abogados")
