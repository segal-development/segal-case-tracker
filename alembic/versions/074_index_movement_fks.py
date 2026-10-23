"""index the foreign keys that point at movements.id and cases.id

Revision ID: 074
Revises: 073

Deleting a movement (or a case) makes PostgreSQL check every table that references
it. Where the referencing column has no index, that check scans the whole table.
Measured on the QA database on 2026-10-23, while running the duplicate-cases merge:

    documents.movement_id            277k rows, 248 MB   no index
    alerts.movement_id               288k rows,  46 MB   no index
    case_deadlines.source_movement_id / verdict_movement_id   10k rows   no index
    cartera_snapshots.case_id         29k rows           no index

Each ``DELETE FROM movements`` took about 1.4 s, a merge of 25 groups about 10 minutes,
and the same scans back the ``documents``/``alerts`` lookups by movement.

Purely additive: indexes only, no schema or data change. Like migration 052 this is a
plain ``CREATE INDEX`` inside the migration transaction (no ``CONCURRENTLY``, which
runs outside it and can leave an invalid index): writes to those tables wait for the
few seconds the build takes. ``IF NOT EXISTS`` / ``IF EXISTS`` make a re-run safe.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "074"
down_revision: Union[str, None] = "073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, column). Names match what ``Column(..., index=True)`` generates.
INDEXES = [
    ("ix_alerts_movement_id", "alerts", "movement_id"),
    ("ix_documents_movement_id", "documents", "movement_id"),
    ("ix_case_deadlines_source_movement_id", "case_deadlines", "source_movement_id"),
    ("ix_case_deadlines_verdict_movement_id", "case_deadlines", "verdict_movement_id"),
    ("ix_cartera_snapshots_case_id", "cartera_snapshots", "case_id"),
]


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    for name, _table, _column in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
