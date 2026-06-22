"""Add next_deadline_fatal column to cases.

Revision ID: 010
Revises: 009
Create Date: 2026-06-22

Fatal deadline flag — True when the nearest actionable deadline is of a type
whose missing would cause irreversible procedural harm to the client:
  - EXCEPCIONES_8D (art. 459 CPC): debtor loses whole defense
  - APELACION_5D (art. 187/475 CPC): sentence becomes firme

Computed by DeadlineEngine.recompute_case on every sync.

Changes:
- cases: add next_deadline_fatal BOOLEAN NOT NULL DEFAULT false
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "next_deadline_fatal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "next_deadline_fatal")
