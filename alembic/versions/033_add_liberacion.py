"""add semáforo override columns + liberacion_requests table

Liberación de causa: a dual-sign-off (auditor + dirección) request to manually
move a case's semáforo, and the override columns the DeadlineEngine honors until
a newer movement supersedes them.

Revision ID: 033
Revises: 032
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("semaforo_override", sa.String(length=10), nullable=True))
    op.add_column("cases", sa.Column("semaforo_override_at", sa.DateTime(), nullable=True))
    op.add_column("cases", sa.Column("semaforo_override_by", sa.String(length=255), nullable=True))

    op.create_table(
        "liberacion_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("requested_by_rut", sa.String(length=20), nullable=True),
        sa.Column("requested_by_name", sa.String(length=255), nullable=True),
        sa.Column("target_semaforo", sa.String(length=10), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("auditor_aprobado_by_rut", sa.String(length=20), nullable=True),
        sa.Column("auditor_aprobado_by_name", sa.String(length=255), nullable=True),
        sa.Column("auditor_aprobado_at", sa.DateTime(), nullable=True),
        sa.Column("direccion_aprobado_by_rut", sa.String(length=20), nullable=True),
        sa.Column("direccion_aprobado_by_name", sa.String(length=255), nullable=True),
        sa.Column("direccion_aprobado_at", sa.DateTime(), nullable=True),
        sa.Column("aplicado_at", sa.DateTime(), nullable=True),
        sa.Column("rechazado_by_rut", sa.String(length=20), nullable=True),
        sa.Column("rechazado_by_name", sa.String(length=255), nullable=True),
        sa.Column("rechazo_motivo", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_liberacion_requests_case_id", "liberacion_requests", ["case_id"])
    op.create_index("ix_liberacion_requests_estado", "liberacion_requests", ["estado"])


def downgrade() -> None:
    op.drop_table("liberacion_requests")
    op.drop_column("cases", "semaforo_override_by")
    op.drop_column("cases", "semaforo_override_at")
    op.drop_column("cases", "semaforo_override")
