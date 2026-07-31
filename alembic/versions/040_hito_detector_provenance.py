"""detector: hito provenance fields (origen/movement_id/regla_code/confianza)

Foundation for the PJUD hito detector (Fase 1). Adds provenance so an
auto-detected candidate carries which movement triggered it, which rule fired,
and the classifier confidence. Existing rows default to origen='manual' via the
server_default. The new estado value 'sugerido' needs no schema change (estado
is a free String).

Revision ID: 040
Revises: 039
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hitos",
        sa.Column("origen", sa.String(length=20), nullable=False, server_default="manual"),
    )
    op.add_column("hitos", sa.Column("movement_id", sa.Integer(), nullable=True))
    op.add_column("hitos", sa.Column("regla_code", sa.String(length=50), nullable=True))
    op.add_column("hitos", sa.Column("confianza", sa.String(length=10), nullable=True))
    op.create_foreign_key(
        "fk_hitos_movement_id", "hitos", "movements", ["movement_id"], ["id"]
    )
    op.create_index("ix_hitos_movement_id", "hitos", ["movement_id"])


def downgrade() -> None:
    op.drop_index("ix_hitos_movement_id", table_name="hitos")
    op.drop_constraint("fk_hitos_movement_id", "hitos", type_="foreignkey")
    op.drop_column("hitos", "confianza")
    op.drop_column("hitos", "regla_code")
    op.drop_column("hitos", "movement_id")
    op.drop_column("hitos", "origen")
