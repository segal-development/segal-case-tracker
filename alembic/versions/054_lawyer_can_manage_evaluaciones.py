"""add can_manage_evaluaciones flag to lawyers

Adds a granular per-user permission ``can_manage_evaluaciones`` to ``lawyers``.
When true, a non-admin lawyer may do everything an admin can inside the
Evaluaciones module (and nothing else). Column is NOT NULL with a ``false``
server default so existing rows are unaffected.

Revision ID: 054
Revises: 053
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: Union[str, None] = "053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lawyers",
        sa.Column(
            "can_manage_evaluaciones",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("lawyers", "can_manage_evaluaciones")
