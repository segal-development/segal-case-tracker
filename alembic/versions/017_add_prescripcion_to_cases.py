"""add prescripcion to cases

Revision ID: 017
Revises: 016
Create Date: 2026-06-24

Adds statute-of-limitations (prescripción) columns to cases table:
  - titulo_tipo: type of executive title (pagare, letra, cheque, escritura_publica, sentencia, otro)
  - titulo_fecha: key date of the title (input for plazo computation)
  - prescripcion_cumplida: advisory flag — True when the plazo has elapsed (computed)
  - prescripcion_fecha: computed date = titulo_fecha + plazo
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("titulo_tipo", sa.String(30), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("titulo_fecha", sa.Date(), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column(
            "prescripcion_cumplida",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "cases",
        sa.Column("prescripcion_fecha", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cases", "prescripcion_fecha")
    op.drop_column("cases", "prescripcion_cumplida")
    op.drop_column("cases", "titulo_fecha")
    op.drop_column("cases", "titulo_tipo")
