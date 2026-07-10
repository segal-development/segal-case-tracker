"""add generated_documents table

Revision ID: 029
Revises: 028
Create Date: 2026-07-10

Creates the ``generated_documents`` table (system requirement #3, slice 2):
one row per legal document the firm GENERATES (escritos/demandas), with
provenance (acting lawyer, firm side) and a content-addressable storage key.
Distinct from ``documents``, which tracks documents DOWNLOADED from PJUD.

Additive and reversible.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generated_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="stored",
            nullable=False,
        ),
        sa.Column("generated_by_rut", sa.String(length=20), nullable=True),
        sa.Column("generated_by_name", sa.String(length=255), nullable=True),
        sa.Column("firm_side", sa.String(length=20), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_documents_case_id",
        "generated_documents",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_generated_documents_id",
        "generated_documents",
        ["id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generated_documents_id", table_name="generated_documents")
    op.drop_index("ix_generated_documents_case_id", table_name="generated_documents")
    op.drop_table("generated_documents")
