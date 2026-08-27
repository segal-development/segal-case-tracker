"""add performance indexes for dashboard/list hot paths

Purely additive DB indexes that back the heaviest read paths, based on a
verified audit against the QA database. No schema or data changes.

- ``alerts (lawyer_id, created_at)``: the alerts list filters by ``lawyer_id``
  and orders by ``created_at DESC``; on ~134k rows this composite is the single
  biggest win.
- ``cases`` single-column btrees on ``court_id``, ``last_movement_at``,
  ``updated_at`` and ``procedure`` for the dashboard scans and joins.
- ``cases`` partial indexes for the boolean quick-filter tabs — each only
  indexes the (small) set of TRUE rows so the risk-board tab filters stay cheap.

Additive and isolated — creates indexes only, no locks beyond a plain
``CREATE INDEX`` (no ``CONCURRENTLY``; Alembic runs inside a transaction).

Revision ID: 052
Revises: 051
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "052"
down_revision: Union[str, None] = "051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # alerts: per-dashboard filter (lawyer_id) + ORDER BY created_at DESC.
    op.create_index(
        "ix_alerts_lawyer_created",
        "alerts",
        ["lawyer_id", "created_at"],
    )

    # cases: single-column btrees for dashboard scans / joins.
    op.create_index("ix_cases_court_id", "cases", ["court_id"])
    op.create_index("ix_cases_last_movement_at", "cases", ["last_movement_at"])
    op.create_index("ix_cases_updated_at", "cases", ["updated_at"])
    op.create_index("ix_cases_procedure", "cases", ["procedure"])

    # cases: partial indexes for the boolean quick-filter tabs. Only the TRUE
    # rows are indexed, so these stay tiny while making each tab's filter a
    # direct index scan.
    op.create_index(
        "ix_cases_abandono",
        "cases",
        ["id"],
        postgresql_where=text("abandono_disponible IS TRUE"),
    )
    op.create_index(
        "ix_cases_apremio",
        "cases",
        ["id"],
        postgresql_where=text("en_apremio IS TRUE"),
    )
    op.create_index(
        "ix_cases_prescripcion",
        "cases",
        ["id"],
        postgresql_where=text("prescripcion_cumplida IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_cases_prescripcion", table_name="cases")
    op.drop_index("ix_cases_apremio", table_name="cases")
    op.drop_index("ix_cases_abandono", table_name="cases")
    op.drop_index("ix_cases_procedure", table_name="cases")
    op.drop_index("ix_cases_updated_at", table_name="cases")
    op.drop_index("ix_cases_last_movement_at", table_name="cases")
    op.drop_index("ix_cases_court_id", table_name="cases")
    op.drop_index("ix_alerts_lawyer_created", table_name="alerts")
