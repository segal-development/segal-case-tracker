"""Backfill case_lawyer_source from Case.lawyer_id.

Revision ID: 073
Revises: 072

The worker path (``SyncService.sync_cases``) wrote ``Case.lawyer_id`` but never
a ``case_lawyer_source`` row, so the ~15k cases owned by individual lawyers
have no sighting for their owner. The worker is about to read "owner OR a
source row" (so cases can later move to the firm owner without losing their
per-lawyer attribution), which requires the rows to exist first.

One ``(case_id, lawyer_id)`` row per existing case, firm-owned cases included,
stamped with the case's ``created_at`` (``now`` when it is NULL). Pairs that
already have a row are left untouched, so this is idempotent and never
overwrites a real sighting.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "073"
down_revision: Union[str, None] = "072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKFILL_SQL = """
INSERT INTO case_lawyer_source (case_id, lawyer_id, first_seen_at, last_seen_at)
SELECT c.id,
       c.lawyer_id,
       COALESCE(c.created_at, CURRENT_TIMESTAMP),
       COALESCE(c.created_at, CURRENT_TIMESTAMP)
FROM cases c
WHERE c.lawyer_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM case_lawyer_source s
      WHERE s.case_id = c.id
        AND s.lawyer_id = c.lawyer_id
  )
"""


def upgrade() -> None:
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """No-op on purpose.

    The backfilled rows are indistinguishable from legitimately observed ones
    (same table, same shape) and are harmless, so there is no safe way to
    tell which ones to remove.
    """
