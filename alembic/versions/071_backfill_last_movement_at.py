"""Backfill Case.last_movement_at from the movements each case actually has.

Revision ID: 071
Revises: 070

The column used to be written from ``scraped_movements[0]`` of whichever
scrape ran last, so it depended on the order PJUD returned rows in and on
that one scrape having seen everything. Neither holds, and several paths
insert Movement rows without touching the column at all. Measured on QA
before this ran: 2.113 active cases carried a last_movement_at OLDER than
their own newest movement row.

``sync_service`` now derives the column from the rows on every sync, so this
only has to repair the accumulated drift once.

Only moves a row FORWARD, and only when the case actually has movements. A
case whose column is already correct, or ahead, or that has no movements, is
left untouched: this repairs drift, it does not reinterpret history.

No downgrade: the previous values were wrong by definition and are not worth
restoring.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "071"
down_revision: Union[str, None] = "070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE cases c
        SET last_movement_at = m.newest
        FROM (
            SELECT case_id, max(movement_date) AS newest
            FROM movements
            WHERE movement_date IS NOT NULL
            GROUP BY case_id
        ) m
        WHERE m.case_id = c.id
          AND (c.last_movement_at IS NULL OR c.last_movement_at < m.newest)
        """
    )


def downgrade() -> None:
    # The values this replaced were wrong; there is nothing to restore.
    pass
