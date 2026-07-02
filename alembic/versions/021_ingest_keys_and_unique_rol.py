"""add ingest_keys table + uq_cases_lawyer_rol unique constraint

Revision ID: 021
Revises: 020
Create Date: 2026-07-02

No dedup step: production data was confirmed to have zero (lawyer_id, rol)
duplicates before this migration was written, so the unique constraint is
applied directly.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_cases_lawyer_rol", "cases", ["lawyer_id", "rol"]
    )

    op.create_table(
        "ingest_keys",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ingest_keys_id", "ingest_keys", ["id"])
    op.create_index(
        "ix_ingest_keys_key_hash", "ingest_keys", ["key_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_keys_key_hash", table_name="ingest_keys")
    op.drop_index("ix_ingest_keys_id", table_name="ingest_keys")
    op.drop_table("ingest_keys")

    op.drop_constraint("uq_cases_lawyer_rol", "cases", type_="unique")
