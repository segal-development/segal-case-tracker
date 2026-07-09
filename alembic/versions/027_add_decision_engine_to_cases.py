"""add decision engine columns to cases

Revision ID: 027
Revises: 026
Create Date: 2026-07-09

Adds the DecisionEngine's denormalized output columns to `cases` (req #5/#11
— motor de decisiones + calendarización), populated by
DeadlineEngine.recompute_case (Step 9, app/services/deadline_engine.py):
  - recommended_action_code: the top-priority DEFENSE action code from
    app/core/decision_rules.py (e.g. "oponer_excepciones"), or NULL when
    nothing is pending.
  - next_review_at: the next date this case should be manually reviewed.

Additive and reversible.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("recommended_action_code", sa.String(50), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("next_review_at", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cases", "next_review_at")
    op.drop_column("cases", "recommended_action_code")
