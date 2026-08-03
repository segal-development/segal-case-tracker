"""bono: meta de cumplimiento del mes (cargada a mano por Carla)

Meta (puntos de %) que se compara con lo que llevan (suma de semanas) para ver
si van a alcanzar el objetivo del mes. Es referencia visual — no altera V3.

Revision ID: 042
Revises: 041
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bono_variables",
        sa.Column("meta_cumplimiento", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("bono_variables", "meta_cumplimiento")
