"""Index movement detection time for the Sysgal daily feed.

Revision ID: 070
Revises: 069
"""

from typing import Sequence, Union

from alembic import op


revision: str = "070"
down_revision: Union[str, None] = "069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_movements_created_at", "movements", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_movements_created_at", table_name="movements")
