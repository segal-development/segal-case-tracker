"""add consulta_reserved to cases

Revision ID: 016
Revises: 015
Create Date: 2026-06-23

Adds consulta_reserved column to cases table to flag cases that are not
visible in PJUD Consulta Unificada (consulta_by_rol returns None), so the
runner can route them to the Mis-Causas rotation instead.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "consulta_reserved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "consulta_reserved")
