"""perf: index the case_id FKs on movements, alerts, documents

These three high-row tables (movements/alerts/documents, ~80-95k rows each)
had NO index on their ``case_id`` foreign key. Every per-case load and every
DELETE on ``cases`` forced a full sequential scan per referenced row — a
2,134-case cleanup timed out because the FK verification scanned ~270k rows
per deleted case. Indexing case_id makes those lookups index-seeks.

Idempotent: the indexes were already created on QA during the cleanup, so we
use ``CREATE INDEX IF NOT EXISTS`` (valid on both Postgres and SQLite).

Revision ID: 039
Revises: 038
"""
from typing import Sequence, Union

from alembic import op

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("movements", "alerts", "documents")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_case_id ON {table}(case_id)")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_case_id")
