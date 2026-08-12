"""redaccion_api_keys: hashed API keys for the external read-only Redaccion API

Slice 1 of the external Redaccion (document-drafters) API. Creates the
``redaccion_api_keys`` table that backs
``app.api.redaccion.deps.require_redaccion_key``. Mirrors ``sysgal_api_keys`` /
``ingest_keys``: only the SHA-256 hash of the key is stored, never the plaintext.

Additive and isolated — touches no existing table.

Revision ID: 046
Revises: 045
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "redaccion_api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_redaccion_api_keys_id"), "redaccion_api_keys", ["id"], unique=False)
    op.create_index(
        op.f("ix_redaccion_api_keys_key_hash"), "redaccion_api_keys", ["key_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_redaccion_api_keys_key_hash"), table_name="redaccion_api_keys")
    op.drop_index(op.f("ix_redaccion_api_keys_id"), table_name="redaccion_api_keys")
    op.drop_table("redaccion_api_keys")
