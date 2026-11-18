"""sysgal_contratos: per-contract mora data from Sysgal /api_sync/cuotas

Revision ID: 089
Revises: 088
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "089"
down_revision: Union[str, None] = "088"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sysgal_contratos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contrato", sa.String(50), nullable=False),
        sa.Column("cuota_actual", sa.Integer(), nullable=True),
        sa.Column("contrato_moroso", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("contrato_al_dia", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("id_causas", sa.JSON(), nullable=False),
        sa.Column("copia_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sysgal_contratos_id", "sysgal_contratos", ["id"])
    op.create_index("ix_sysgal_contratos_contrato", "sysgal_contratos", ["contrato"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_sysgal_contratos_contrato", table_name="sysgal_contratos")
    op.drop_index("ix_sysgal_contratos_id", table_name="sysgal_contratos")
    op.drop_table("sysgal_contratos")
