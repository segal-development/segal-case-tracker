"""sysgal_causas.matriz (Sysgal's own matriz, when its API sends one)

Revision ID: 084
Revises: 083
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "084"
down_revision: Union[str, None] = "083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sysgal_causas", sa.Column("matriz", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("sysgal_causas", "matriz")
