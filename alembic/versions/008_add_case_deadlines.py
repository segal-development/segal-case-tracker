"""Add case_deadlines table and procedural deadline columns to cases.

Revision ID: 008
Revises: 007
Create Date: 2026-06-16

Semáforo de Plazos (Slice A, PR1): stores computed procedural deadlines for
civil cases (juicio ejecutivo) so the API and list view can surface urgency
without an N+1 query.

Changes:
- New table: case_deadlines
    id, case_id FK, deadline_type, legal_basis, due_date DATE,
    triggered_at DATE, status VARCHAR(20) DEFAULT 'active',
    source_movement_id FK (nullable), computed_at, created_at
    UNIQUE(case_id, deadline_type, triggered_at)
    Indexes: ix_case_deadlines_case_id, ix_case_deadlines_due_date
- cases: add procedural_state VARCHAR(30) nullable
- cases: add semaforo VARCHAR(10) nullable
- cases: add next_deadline_at DATE nullable
- Indexes: ix_cases_next_deadline_at, ix_cases_semaforo

Rollback: drop indexes → drop case_deadlines → drop 3 Case columns.
All new columns are nullable / have defaults → no data loss on downgrade.

COORDINATION NOTE: prospect-search branch claims migrations 008 and 009.
If that branch merges to main before this one, renumber this file to 009
and set down_revision="008" (or the last merged revision).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create case_deadlines table
    # ------------------------------------------------------------------
    op.create_table(
        "case_deadlines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "case_id",
            sa.Integer,
            sa.ForeignKey("cases.id"),
            nullable=False,
        ),
        sa.Column("deadline_type", sa.String(50), nullable=False),
        sa.Column("legal_basis", sa.String(100), nullable=True),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("triggered_at", sa.Date, nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "source_movement_id",
            sa.Integer,
            sa.ForeignKey("movements.id"),
            nullable=True,
        ),
        sa.Column("computed_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=True,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "case_id",
            "deadline_type",
            "triggered_at",
            name="uq_case_deadline_type_triggered",
        ),
    )

    op.create_index(
        "ix_case_deadlines_case_id",
        "case_deadlines",
        ["case_id"],
    )
    op.create_index(
        "ix_case_deadlines_due_date",
        "case_deadlines",
        ["due_date"],
    )

    # ------------------------------------------------------------------
    # 2. Add procedural deadline columns to cases
    # ------------------------------------------------------------------
    op.add_column(
        "cases",
        sa.Column("procedural_state", sa.String(30), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("semaforo", sa.String(10), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("next_deadline_at", sa.Date, nullable=True),
    )

    op.create_index(
        "ix_cases_next_deadline_at",
        "cases",
        ["next_deadline_at"],
    )
    op.create_index(
        "ix_cases_semaforo",
        "cases",
        ["semaforo"],
    )


def downgrade() -> None:
    # Drop indexes first (required by some DB engines before dropping columns)
    op.drop_index("ix_cases_semaforo", table_name="cases")
    op.drop_index("ix_cases_next_deadline_at", table_name="cases")
    op.drop_column("cases", "next_deadline_at")
    op.drop_column("cases", "semaforo")
    op.drop_column("cases", "procedural_state")

    op.drop_index("ix_case_deadlines_due_date", table_name="case_deadlines")
    op.drop_index("ix_case_deadlines_case_id", table_name="case_deadlines")
    op.drop_table("case_deadlines")
