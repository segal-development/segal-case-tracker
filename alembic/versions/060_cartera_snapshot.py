"""cartera snapshot — frozen monthly portfolio for tramitación KPIs

"Cartera del mes = snapshot del día 1 a las 00:00 hrs": every tramitación KPI
must be computed against a FROZEN monthly portfolio, not against whatever the
``cases`` table currently looks like — otherwise a mid-month reassignment or
a nivel change would silently rewrite last month's numbers.

- ``cartera_snapshots``: one row per civil causa per period, freezing the
  resolved owner (``app.services.lawyer_roster.resolved_owner_by_case`` —
  asignación override > litigante of record > nothing; NEVER ``Case.lawyer_id``,
  which is scraping provenance only), the owner's ``nivel``, the matriz
  fields, and freshness evidence (``last_movement_at`` /
  ``last_detail_checked_at``) — all frozen at snapshot time so they never
  drift with later changes. Detail scraping coverage is uneven across the
  portfolio (see ``app.services.matriz_classifier`` docstring), so the
  freshness evidence travels WITH every row, not as a separate join.
- ``cartera_snapshot_runs``: one row per period a snapshot was taken for —
  the idempotency/audit log ``tomar_snapshot`` checks before rebuilding.

Revision ID: 060
Revises: 059
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: Union[str, None] = "059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cartera_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("periodo", sa.String(length=7), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("lawyer_id", sa.Integer(), nullable=True),
        sa.Column("nivel", sa.String(length=10), nullable=True),
        sa.Column("matriz", sa.String(length=20), nullable=True),
        sa.Column("matriz_etapa", sa.String(length=80), nullable=True),
        sa.Column("matriz_origen", sa.String(length=30), nullable=True),
        sa.Column("last_movement_at", sa.DateTime(), nullable=True),
        sa.Column("last_detail_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["lawyer_id"], ["lawyers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("periodo", "case_id", name="uq_cartera_snapshots_periodo_case"),
    )
    op.create_index("ix_cartera_snapshots_id", "cartera_snapshots", ["id"], unique=False)
    op.create_index("ix_cartera_snapshots_periodo", "cartera_snapshots", ["periodo"], unique=False)
    op.create_index(
        "ix_cartera_snapshots_periodo_lawyer", "cartera_snapshots", ["periodo", "lawyer_id"], unique=False
    )
    op.create_index(
        "ix_cartera_snapshots_periodo_matriz", "cartera_snapshots", ["periodo", "matriz"], unique=False
    )

    op.create_table(
        "cartera_snapshot_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("periodo", sa.String(length=7), nullable=False),
        sa.Column("tomado_at", sa.DateTime(), nullable=False),
        sa.Column("causas", sa.Integer(), nullable=False),
        sa.Column("tomado_por", sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("periodo", name="uq_cartera_snapshot_runs_periodo"),
    )
    op.create_index("ix_cartera_snapshot_runs_id", "cartera_snapshot_runs", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cartera_snapshot_runs_id", table_name="cartera_snapshot_runs")
    op.drop_table("cartera_snapshot_runs")

    op.drop_index("ix_cartera_snapshots_periodo_matriz", table_name="cartera_snapshots")
    op.drop_index("ix_cartera_snapshots_periodo_lawyer", table_name="cartera_snapshots")
    op.drop_index("ix_cartera_snapshots_periodo", table_name="cartera_snapshots")
    op.drop_index("ix_cartera_snapshots_id", table_name="cartera_snapshots")
    op.drop_table("cartera_snapshots")
