"""add read/read_at to alerts

Revision ID: 026
Revises: 025
Create Date: 2026-07-08

Adds the in-app alert feed read/unread state to `alerts`: `read` (boolean,
defaults false) and `read_at` (nullable timestamp, set when the lawyer
dismisses the alert in-app). Additive and reversible.

The server_default on `read` backfills every pre-existing row to `read=False`
at the same time the column is added — no separate data migration needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column(
            "read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "alerts",
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "read_at")
    op.drop_column("alerts", "read")
