"""activity_events: who saw and did what, visible to admins

Revision ID: 076
Revises: 075

Purely additive. Never stores bodies, query strings, tokens, passwords or keys.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "076"
down_revision: Union[str, None] = "075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
        sa.Column("actor_name", sa.String(255), nullable=True),
        sa.Column("actor_role", sa.String(20), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("route", sa.String(255), nullable=False),
        sa.Column("path", sa.String(255), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("target", sa.String(255), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("identifier", sa.String(120), nullable=True),
    )
    op.create_index("ix_activity_events_id", "activity_events", ["id"])
    op.create_index("ix_activity_events_created_at", "activity_events", ["created_at"])
    op.create_index("ix_activity_events_case_id", "activity_events", ["case_id"])
    op.create_index(
        "ix_activity_events_lawyer_created", "activity_events", ["lawyer_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("activity_events")
