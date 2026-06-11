"""Add document token columns to documents and movements tables.

Revision ID: 006
Revises: 005
Create Date: 2026-06-11

Slice 1 (case-documents): Captures PJUD document JWTs in the database.

Changes:
- documents.doc_type          VARCHAR(50)   nullable
- documents.pjud_endpoint     VARCHAR(255)  nullable
- documents.pjud_token        TEXT          nullable (live JWT, refreshed each sync)
- documents.pjud_token_hash   VARCHAR(64)   nullable (stable identity SHA-256, unique where not null)
- documents.status            VARCHAR(20)   NOT NULL DEFAULT 'pending'
- documents.escrito_id        INTEGER       nullable FK → case_escritos.id
- documents.filename          make nullable (download has not happened at persist time)
- movements.document_token    TEXT          nullable (bug fix: primary doc JWT was silently dropped)

All columns are additive and nullable (except status with server default).
Rollback: alembic downgrade -1 (drops all added columns, no data loss).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- documents table ---

    op.add_column(
        "documents",
        sa.Column("doc_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("pjud_endpoint", sa.String(255), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("pjud_token", sa.Text, nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("pjud_token_hash", sa.String(64), nullable=True),
    )

    # Partial unique index: WHERE pjud_token_hash IS NOT NULL.
    # Allows multiple NULL rows (docs not yet hashed) while enforcing
    # uniqueness on real hashes.
    op.create_index(
        "ix_documents_pjud_token_hash",
        "documents",
        ["pjud_token_hash"],
        unique=True,
        postgresql_where=sa.text("pjud_token_hash IS NOT NULL"),
    )

    op.add_column(
        "documents",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "escrito_id",
            sa.Integer,
            sa.ForeignKey("case_escritos.id"),
            nullable=True,
        ),
    )

    # Make filename nullable (document not yet downloaded at persist time)
    op.alter_column("documents", "filename", nullable=True)

    # --- movements table ---

    op.add_column(
        "movements",
        sa.Column("document_token", sa.Text, nullable=True),
    )


def downgrade() -> None:
    # movements
    op.drop_column("movements", "document_token")

    # documents — drop in reverse order of upgrade
    op.alter_column("documents", "filename", nullable=False)
    op.drop_column("documents", "escrito_id")
    op.drop_column("documents", "status")
    op.drop_index("ix_documents_pjud_token_hash", table_name="documents")
    op.drop_column("documents", "pjud_token_hash")
    op.drop_column("documents", "pjud_token")
    op.drop_column("documents", "pjud_endpoint")
    op.drop_column("documents", "doc_type")
