"""cases.patrocinio_pendiente + sysgal_incorporaciones.ultimo_intento_at

Revision ID: 082
Revises: 081
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "082"
down_revision: Union[str, None] = "081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("patrocinio_pendiente", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("sysgal_incorporaciones", sa.Column("ultimo_intento_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_sysgal_incorporaciones_ultimo_intento_at", "sysgal_incorporaciones", ["ultimo_intento_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_sysgal_incorporaciones_ultimo_intento_at", table_name="sysgal_incorporaciones")
    op.drop_column("sysgal_incorporaciones", "ultimo_intento_at")
    op.drop_column("cases", "patrocinio_pendiente")
