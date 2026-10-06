"""marca de origen de las filas de case_deadlines

Las filas de ``case_deadlines`` las crea el motor (o el auditor a mano). Un
backfill puntual que crea filas FUERA del motor necesita poder deshacer
exactamente lo que creo, sin adivinar por fechas ni por forma de la fila. Esta
columna guarda el nombre del trabajo que la creo; NULL = motor o auditor.

Una columna nullable, SIN default: en Postgres es un cambio solo de catalogo (no
reescribe filas). Sin indice: solo la lee el script de reversion, una vez.

Revision ID: 064
Revises: 063
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("case_deadlines", sa.Column("origin", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("case_deadlines", "origin")
