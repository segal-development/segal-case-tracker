"""add presentación cross-review audit fields (Slice 1c)

Adds the two audit columns that record the cross-review (revisión cruzada /
four-eyes) of a loaded filing before its final send. When a filing at
``cargada_pendiente_envio`` is reviewed via ``POST /presentaciones/{id}/revisar``
it moves to the new ``revisado`` state; ``revisado_por`` / ``revisado_at``
capture who cross-reviewed it (a person distinct from the sender) and when. The
send (``POST /presentaciones/{id}/enviar``) now requires a prior ``revisado``.
No new state column — ``estado`` already stores the string value.

Additive and isolated — only adds two nullable columns to ``presentaciones``.

Revision ID: 051
Revises: 050
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "051"
down_revision: Union[str, None] = "050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "presentaciones",
        sa.Column("revisado_por", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "presentaciones",
        sa.Column("revisado_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("presentaciones", "revisado_at")
    op.drop_column("presentaciones", "revisado_por")
