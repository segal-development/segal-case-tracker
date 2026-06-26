"""add pending_connections table

Revision ID: 020
Revises: 019
Create Date: 2026-06-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_connections",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column(
            "lawyer_id",
            sa.Integer(),
            sa.ForeignKey("lawyers.id"),
            nullable=False,
        ),
        sa.Column("rut", sa.String(12), nullable=False),
        sa.Column("auth_method", sa.String(50), nullable=False),
        sa.Column("captcha_token", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("cases_synced", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("picked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pending_connections_id", "pending_connections", ["id"])
    op.create_index(
        "ix_pending_connections_connection_id",
        "pending_connections",
        ["connection_id"],
        unique=True,
    )
    op.create_index(
        "ix_pending_connections_status", "pending_connections", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_pending_connections_status", table_name="pending_connections")
    op.drop_index(
        "ix_pending_connections_connection_id", table_name="pending_connections"
    )
    op.drop_index("ix_pending_connections_id", table_name="pending_connections")
    op.drop_table("pending_connections")
