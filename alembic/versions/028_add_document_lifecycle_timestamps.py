"""add document lifecycle timestamps (stored_at, failed_at)

Revision ID: 028
Revises: 027
Create Date: 2026-07-09

Adds `Document.stored_at` and `Document.failed_at` (nullable DateTime).
`downloaded_at` is set at ROW CREATION (not at actual download completion),
so today there is no timestamp recording when a document transitioned to
`stored`/`failed`. These two columns fill that gap for the case lifecycle
timeline (req #8, inbound/operational slice).

Additive and reversible.

Backfill: existing `status='stored'` documents get `stored_at =
downloaded_at` so they already have a usable timeline timestamp without
requiring a re-download.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_stored_at(bind) -> None:
    """Set stored_at = downloaded_at for existing stored docs missing it."""
    bind.execute(
        sa.text(
            "UPDATE documents SET stored_at = downloaded_at "
            "WHERE status = 'stored' AND stored_at IS NULL"
        )
    )


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("stored_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("failed_at", sa.DateTime(), nullable=True),
    )
    _backfill_stored_at(op.get_bind())


def downgrade() -> None:
    op.drop_column("documents", "failed_at")
    op.drop_column("documents", "stored_at")
