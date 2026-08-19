"""add tribunal to hitos (part of the causa identity for dedup)

A hito references a causa by CLIENT RUT (``rol_causa``) + ROL (``descripcion``).
But the same ROL can exist in DIFFERENT tribunals for the same client, so ROL
alone does not identify a causa: two such hitos are legitimately distinct and
both payable. Add a nullable ``tribunal`` column so the dedup key can be
``(lawyer, rol_causa, ROL, tribunal)`` instead of wrongly collapsing them.

Additive — nullable column, no backfill needed (existing rows keep NULL).

Revision ID: 048
Revises: 047
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: Union[str, None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hitos", sa.Column("tribunal", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("hitos", "tribunal")
