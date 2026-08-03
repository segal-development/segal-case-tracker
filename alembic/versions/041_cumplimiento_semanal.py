"""bono: avance de cartera semanal para V3 (cumpl_sem1..5)

V3 (cumplimiento mensual) pasa a alimentarse del avance de cartera cargado por
SEMANA (puntos de %, ej. 7.5); el % del mes es la suma de las semanas. Las
columnas legacy causas_asignadas/cumplidas se conservan para historial pero ya
no se editan. Filas existentes quedan con 0 en cada semana (server_default).

Revision ID: 041
Revises: 040
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = ("cumpl_sem1", "cumpl_sem2", "cumpl_sem3", "cumpl_sem4", "cumpl_sem5")


def upgrade() -> None:
    for col in _COLS:
        op.add_column(
            "bono_variables",
            sa.Column(col, sa.Float(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for col in _COLS:
        op.drop_column("bono_variables", col)
