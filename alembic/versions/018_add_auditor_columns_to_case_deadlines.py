"""add auditor columns to case_deadlines

Revision ID: 018
Revises: 017
Create Date: 2026-06-25

Adds three columns to the case_deadlines table to support auditor workflow:
  - is_manual:  flags deadlines created manually by an auditor (default False)
  - marked_by:  FK to lawyers.id — the auditor who last changed the status
  - marked_at:  timestamp of the last auditor status change
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "case_deadlines",
        sa.Column(
            "is_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "case_deadlines",
        sa.Column(
            "marked_by",
            sa.Integer(),
            sa.ForeignKey("lawyers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "case_deadlines",
        sa.Column("marked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("case_deadlines", "marked_at")
    op.drop_column("case_deadlines", "marked_by")
    op.drop_column("case_deadlines", "is_manual")
