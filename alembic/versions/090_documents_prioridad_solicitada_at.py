"""documents.prioridad_solicitada_at: priority-download request timestamp

Revision ID: 090
Revises: 089
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "090"
down_revision: Union[str, None] = "089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("prioridad_solicitada_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_documents_prioridad_solicitada_at",
        "documents",
        ["prioridad_solicitada_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_prioridad_solicitada_at", table_name="documents")
    op.drop_column("documents", "prioridad_solicitada_at")
