"""document full-text search: texto + text_extracted_at + Spanish GIN index

Slice 1 of document full-text search. Adds two columns to ``documents``:

- ``texto``            — extracted plain text of the PDF (nullable).
- ``text_extracted_at``— when the extraction ran (nullable).

On PostgreSQL it also creates a Spanish full-text GIN index over
``to_tsvector('spanish', coalesce(texto, ''))`` so the search endpoint can use
``@@ websearch_to_tsquery('spanish', :q)`` with ``ts_rank`` ordering. The index
is PostgreSQL-only; on SQLite (tests) search falls back to a LIKE scan and no
GIN index is created.

Revision ID: 044
Revises: 043
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("texto", sa.Text(), nullable=True))
    op.add_column(
        "documents", sa.Column("text_extracted_at", sa.DateTime(), nullable=True)
    )

    # Spanish full-text GIN index — PostgreSQL only. SQLite has no GIN/to_tsvector
    # (search falls back to LIKE there), so skip index creation on it.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_documents_texto_fts ON documents "
            "USING GIN (to_tsvector('spanish', coalesce(texto, '')))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_documents_texto_fts")

    op.drop_column("documents", "text_extracted_at")
    op.drop_column("documents", "texto")
