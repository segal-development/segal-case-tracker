"""when a person was blocked and when her password last changed

Revision ID: 075
Revises: 074

The admin screen "Cuentas del estudio" shows "Bloqueada desde {fecha}" and
"Cambiada {fecha}". Both columns are nullable and need no backfill: a NULL
``password_changed_at`` means "unknown" for accounts that already had a password,
and a NULL ``blocked_at`` means not blocked. Purely additive.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "075"
down_revision: Union[str, None] = "074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lawyers", sa.Column("blocked_at", sa.DateTime(), nullable=True))
    op.add_column("lawyers", sa.Column("password_changed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("lawyers", "password_changed_at")
    op.drop_column("lawyers", "blocked_at")
