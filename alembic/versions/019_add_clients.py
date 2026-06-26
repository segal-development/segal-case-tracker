"""add clients table and cases.client_id foreign key

Revision ID: 019
Revises: 018
Create Date: 2026-06-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create clients table first (FK source for cases.client_id)
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("rut", sa.String(12), nullable=True),
        sa.Column("nombre", sa.String(255), nullable=True),
        sa.Column("clave_unica_rut", sa.String(12), nullable=True),
        sa.Column("encrypted_clave_unica_password", sa.String(512), nullable=True),
        sa.Column(
            "assigned_lawyer_id",
            sa.Integer(),
            sa.ForeignKey("lawyers.id"),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "source",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'crm'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_clients_id", "clients", ["id"])
    op.create_index("ix_clients_rut", "clients", ["rut"])
    op.create_index("ix_clients_assigned_lawyer_id", "clients", ["assigned_lawyer_id"])

    # 2. Add client_id FK column to cases
    op.add_column(
        "cases",
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_cases_client_id", "cases", ["client_id"])


def downgrade() -> None:
    # Reverse order: drop cases.client_id before clients table
    op.drop_index("ix_cases_client_id", table_name="cases")
    op.drop_column("cases", "client_id")

    op.drop_index("ix_clients_assigned_lawyer_id", table_name="clients")
    op.drop_index("ix_clients_rut", table_name="clients")
    op.drop_index("ix_clients_id", table_name="clients")
    op.drop_table("clients")
