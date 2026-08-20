"""add presentación confirm-send audit fields (Slice 1b)

Adds the two audit columns that record the redactor's confirmation of the final
send (Opción 2 / semiauto). When a filing at ``cargada_pendiente_envio`` is
confirmed via ``POST /presentaciones/{id}/enviar`` it moves to the new
``envio_confirmado`` state; ``confirmado_por`` / ``confirmado_at`` capture who
authorized the irreversible send and when. The actual OJV "Enviar" is performed
later by the station worker (a future slice). No new state column — ``estado``
already stores the string value.

Additive and isolated — only adds two nullable columns to ``presentaciones``.

Revision ID: 050
Revises: 049
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "presentaciones",
        sa.Column("confirmado_por", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "presentaciones",
        sa.Column("confirmado_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("presentaciones", "confirmado_at")
    op.drop_column("presentaciones", "confirmado_por")
