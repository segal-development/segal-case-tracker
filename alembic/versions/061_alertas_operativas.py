"""alertas operativas — bitácora de los avisos que el sistema envió

No es una bitácora decorativa: es lo que permite avisar UNA vez. Una alerta
que se repite en cada ciclo del worker se vuelve ruido, y el ruido se ignora
— exactamente lo que pasó con ``sync_history``, donde el dato de 29 corridas
fallidas estaba y nadie lo miraba.

Una fila por aviso enviado, no una por tipo: saber cuántas veces y cuándo se
avisó es tan útil como saber que se avisó.

Revision ID: 061
Revises: 060
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "061"
down_revision: Union[str, None] = "060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alertas_operativas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=50), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=True),
        sa.Column("detalle", sa.String(length=500), nullable=True),
        sa.Column("enviada_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alertas_operativas_id", "alertas_operativas", ["id"])
    # La consulta del cooldown es siempre "el último aviso de este tipo".
    op.create_index(
        "ix_alertas_operativas_tipo_enviada",
        "alertas_operativas",
        ["tipo", "enviada_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alertas_operativas_tipo_enviada", table_name="alertas_operativas")
    op.drop_index("ix_alertas_operativas_id", table_name="alertas_operativas")
    op.drop_table("alertas_operativas")
