"""Add abandono_disponible column to cases.

Revision ID: 009
Revises: 008
Create Date: 2026-06-22

Art. 152/153 CPC abandono del procedimiento advisory signal.
Computed by DeadlineEngine.recompute_case on every sync.

Changes:
- cases: add abandono_disponible BOOLEAN NOT NULL DEFAULT false
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "abandono_disponible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "abandono_disponible")
