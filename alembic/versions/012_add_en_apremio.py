"""Add en_apremio column to cases.

Revision ID: 012
Revises: 011
Create Date: 2026-06-22

Engine-computed boolean that signals a juicio ejecutivo has entered the
cuaderno de apremio phase (2nd enforcement cuaderno) based on movement
keywords: embargo / remate / martillero in description, or 'apremio'/'remate'
in stage.

Computed by DeadlineEngine.recompute_case on every sync.

Changes:
- cases: add en_apremio BOOLEAN NOT NULL DEFAULT false
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "en_apremio",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "en_apremio")
