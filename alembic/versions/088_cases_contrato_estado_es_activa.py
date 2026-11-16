"""cases.contrato_estado + cases.es_activa (one definition of "causa activa")

Revision ID: 088
Revises: 087
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "088"
down_revision: Union[str, None] = "087"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("contrato_estado", sa.String(10), nullable=True))
    op.add_column(
        "cases",
        sa.Column("es_activa", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_cases_contrato_estado", "cases", ["contrato_estado"])
    op.create_index("ix_cases_es_activa", "cases", ["es_activa"])
    # contrato_estado starts NULL (= sin_dato, counts as activa) until scripts/recalcular_contrato_estado.py
    # runs; only the terminada rule can be applied from data already in the table.
    op.execute("UPDATE cases SET es_activa = false WHERE terminada_en_sysgal")


def downgrade() -> None:
    op.drop_index("ix_cases_es_activa", table_name="cases")
    op.drop_index("ix_cases_contrato_estado", table_name="cases")
    op.drop_column("cases", "es_activa")
    op.drop_column("cases", "contrato_estado")
