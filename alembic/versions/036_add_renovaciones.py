"""add renovaciones table (contract renewals module)

New module: log the firm's client contract renewals (contract #, client RUT +
name, handling lawyer). Derived: fecha_hasta = fecha_desde + 1 year; total =
monto_cuota * 12.

Revision ID: 036
Revises: 035
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "renovaciones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("numero_contrato", sa.String(length=50), nullable=False),
        sa.Column("cliente_rut", sa.String(length=20), nullable=False),
        sa.Column("cliente_nombre", sa.String(length=255), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("fecha_desde", sa.Date(), nullable=False),
        sa.Column("fecha_hasta", sa.Date(), nullable=False),
        sa.Column("monto_cuota", sa.Integer(), nullable=False),
        sa.Column("cuotas", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("created_by_rut", sa.String(length=20), nullable=True),
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_renovaciones_numero_contrato", "renovaciones", ["numero_contrato"])
    op.create_index("ix_renovaciones_cliente_rut", "renovaciones", ["cliente_rut"])
    op.create_index("ix_renovaciones_lawyer_id", "renovaciones", ["lawyer_id"])
    op.create_index("ix_renovaciones_fecha_desde", "renovaciones", ["fecha_desde"])


def downgrade() -> None:
    op.drop_table("renovaciones")
