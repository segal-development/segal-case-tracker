"""add role to lawyer

Revision ID: 014
Revises: 013
Create Date: 2026-06-23

Adds role column to lawyers table for role-based access control.
Default value is 'lawyer'; existing rows are set to 'lawyer' automatically
via server_default.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lawyers",
        sa.Column("role", sa.String(20), nullable=False, server_default="lawyer"),
    )


def downgrade() -> None:
    op.drop_column("lawyers", "role")
