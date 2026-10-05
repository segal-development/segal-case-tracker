"""veredicto del plazo — si la obligacion se cumplio, cuando y con que prueba

``case_deadlines.status`` es el ciclo de vida de la fila (active / superseded /
expired) y ademas lo usa el auditor para marcar cumplido / no_cumplido a mano.
Cuando la causa avanza, el motor marca el plazo ``superseded`` y hasta ahora no
quedaba registro de si se cumplio. El veredicto es OTRO eje y vive en columnas
propias para que sobreviva al supersede y no se mezcle con las marcas del
auditor.

Cuatro columnas nullable, SIN default: en Postgres agregar una columna nullable
sin default es un cambio solo de catalogo (no reescribe filas). NULL en
``verdict`` significa "sin determinar".

- verdict: cumplido | fuera_de_plazo | no_cumplido | presentado_sin_ancla
- verdict_movement_id: movimiento que prueba el acto (ON DELETE SET NULL: la
  fusion de causas borra movimientos duplicados)
- verdict_acted_on: fecha del acto
- verdict_computed_at: cuando se calculo

Sin indice: ningun lector filtra por veredicto todavia. Cuando la API lo haga,
el indice se decide con la consulta real a la vista.

Revision ID: 063
Revises: 062
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "063"
down_revision: Union[str, None] = "062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("case_deadlines", sa.Column("verdict", sa.String(length=20), nullable=True))
    op.add_column(
        "case_deadlines",
        sa.Column(
            "verdict_movement_id",
            sa.Integer(),
            sa.ForeignKey("movements.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("case_deadlines", sa.Column("verdict_acted_on", sa.Date(), nullable=True))
    op.add_column("case_deadlines", sa.Column("verdict_computed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("case_deadlines", "verdict_computed_at")
    op.drop_column("case_deadlines", "verdict_acted_on")
    op.drop_column("case_deadlines", "verdict_movement_id")
    op.drop_column("case_deadlines", "verdict")
