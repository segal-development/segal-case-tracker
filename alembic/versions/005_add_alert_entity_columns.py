"""Add entity_type and entity_id columns to alerts table.

Revision ID: 005
Revises: 004
Create Date: 2026-06-11

ADR-003: Polymorphic alert target — one (entity_type, entity_id) pair replaces
the four-nullable-FK design rejected in the design phase.  The existing
movement_id column is left untouched (movement alerts continue to use it).
Both new columns are nullable so existing alert rows are unaffected on upgrade.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column("entity_type", sa.String(30), nullable=True),
    )
    op.add_column(
        "alerts",
        sa.Column("entity_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "entity_id")
    op.drop_column("alerts", "entity_type")
