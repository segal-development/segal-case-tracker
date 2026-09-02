"""add cliente_sysgal_estados cache table

Per-RUT cache of the Sysgal CRM commercial state for demandados of causas in
abandono/apremio/prescripción. Populated by ``sync_sysgal_estados`` (worker
hook + admin endpoint) and read by the Causas list to derive the "cobertura"
tag. Stores NO PII (nombre/email/telefono are deliberately not persisted).

Revision ID: 055
Revises: 054
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: Union[str, None] = "054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cliente_sysgal_estados",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rut", sa.String(length=20), nullable=False),
        sa.Column("encontrado", sa.Boolean(), nullable=False),
        sa.Column("estado_codigo", sa.String(length=30), nullable=True),
        sa.Column("estado_label", sa.String(length=60), nullable=True),
        sa.Column("tiene_contrato", sa.Boolean(), nullable=True),
        sa.Column("vigencia_hasta", sa.Date(), nullable=True),
        sa.Column("sysgal_updated_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cliente_sysgal_estados_id", "cliente_sysgal_estados", ["id"], unique=False
    )
    op.create_index(
        "ix_cliente_sysgal_estados_rut", "cliente_sysgal_estados", ["rut"], unique=True
    )
    op.create_index(
        "ix_cliente_sysgal_estados_estado_codigo",
        "cliente_sysgal_estados",
        ["estado_codigo"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cliente_sysgal_estados_estado_codigo", table_name="cliente_sysgal_estados")
    op.drop_index("ix_cliente_sysgal_estados_rut", table_name="cliente_sysgal_estados")
    op.drop_index("ix_cliente_sysgal_estados_id", table_name="cliente_sysgal_estados")
    op.drop_table("cliente_sysgal_estados")
