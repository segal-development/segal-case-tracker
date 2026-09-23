"""asignación por nivel — split "who works this causa" from lawyer_id

Adds an optional override to ``cases`` for the internal (junior/pleno/senior)
lawyer who currently works the causa, independent of ``lawyer_id`` (sync/
provenance — see ``app.services.sync_service.SyncService.sync_cases`` /
``existing_by_rol``, which must keep using ``lawyer_id`` unchanged) and of
the litigante-derived legal attribution (Approach C / ``resolve_case_scope``).

``assigned_lawyer_id`` is deliberately left NULL for every existing row
(NULL means "not reassigned yet" — see ``Case.effective_lawyer_id``), so
this migration only adds columns/indexes and is instant even on the full
``cases`` table (~14.5k rows).

Revision ID: 059
Revises: 058
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "059"
down_revision: Union[str, None] = "058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("assigned_lawyer_id", sa.Integer(), nullable=True))
    op.add_column("cases", sa.Column("assigned_at", sa.DateTime(), nullable=True))
    op.add_column("cases", sa.Column("assigned_by_rut", sa.String(length=20), nullable=True))
    op.add_column("cases", sa.Column("assigned_motivo", sa.String(length=255), nullable=True))

    op.create_foreign_key(
        "fk_cases_assigned_lawyer_id_lawyers",
        "cases",
        "lawyers",
        ["assigned_lawyer_id"],
        ["id"],
    )
    op.create_index(
        "ix_cases_assigned_lawyer_id", "cases", ["assigned_lawyer_id"], unique=False
    )
    op.create_index(
        "ix_cases_assigned_lawyer_matriz",
        "cases",
        ["assigned_lawyer_id", "matriz"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cases_assigned_lawyer_matriz", table_name="cases")
    op.drop_index("ix_cases_assigned_lawyer_id", table_name="cases")
    op.drop_constraint("fk_cases_assigned_lawyer_id_lawyers", "cases", type_="foreignkey")
    op.drop_column("cases", "assigned_motivo")
    op.drop_column("cases", "assigned_by_rut")
    op.drop_column("cases", "assigned_at")
    op.drop_column("cases", "assigned_lawyer_id")
