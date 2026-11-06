"""cases.esperando_tribunal: prueba plazos lapsed and the court idle

Revision ID: 076
Revises: 075

A case whose only red cause is a lapsed prueba-stage plazo is "esperando al tribunal".
The engine computes the flag on every recompute. Additive, defaults to false; existing rows
are filled by a recompute, not by this migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "076"
down_revision: Union[str, None] = "075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("esperando_tribunal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("cases", "esperando_tribunal")
