"""cases.semaforo_motivo (why the engine forced the semáforo: abandono / plazo fatal vencido)

Revision ID: 087
Revises: 086
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "087"
down_revision: Union[str, None] = "086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("semaforo_motivo", sa.String(255), nullable=True))
    # Fase 0: one escalation alert per (deadline, recipient); makes the insert race-safe.
    op.create_index(
        "uq_alerts_plazo_fatal_escalado",
        "alerts",
        ["entity_type", "entity_id", "type", "lawyer_id"],
        unique=True,
        postgresql_where=sa.text("entity_type = 'plazo_fatal_escalado'"),
    )


def downgrade() -> None:
    op.drop_index("uq_alerts_plazo_fatal_escalado", table_name="alerts")
    op.drop_column("cases", "semaforo_motivo")
