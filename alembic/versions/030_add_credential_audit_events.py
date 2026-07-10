"""add credential_audit_events table

Revision ID: 030
Revises: 029
Create Date: 2026-07-10

Creates the ``credential_audit_events`` table: an append-only audit trail for
the read-only credential-monitoring module ("bóveda de credenciales").

SECURITY: this table NEVER stores a credential value. The only
credential-derived column is ``fingerprint`` = ``sha256(ciphertext)`` — a hash
of the ALREADY-ENCRYPTED blob (``lawyers.encrypted_*_password``), which reveals
nothing about the plaintext and is safe to store/compare.

Additive and reversible.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "credential_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), nullable=False),
        sa.Column("credential_type", sa.String(length=20), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lawyer_id"], ["lawyers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credential_audit_events_id",
        "credential_audit_events",
        ["id"],
        unique=False,
    )
    op.create_index(
        "ix_credential_audit_events_lawyer_id",
        "credential_audit_events",
        ["lawyer_id"],
        unique=False,
    )
    op.create_index(
        "ix_credential_audit_events_occurred_at",
        "credential_audit_events",
        ["occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_credential_audit_events_occurred_at",
        table_name="credential_audit_events",
    )
    op.drop_index(
        "ix_credential_audit_events_lawyer_id",
        table_name="credential_audit_events",
    )
    op.drop_index(
        "ix_credential_audit_events_id",
        table_name="credential_audit_events",
    )
    op.drop_table("credential_audit_events")
