"""Add case detail entity tables (litigantes, notificaciones, escritos, exhortos)

Revision ID: 004
Revises: 003
Create Date: 2026-06-11 00:00:00.000000

Slice 1b persistence layer: 4 new tables each with a natural_key column and
UNIQUE(case_id, natural_key) as the idempotent upsert spine (ADR-002).
No Alert column changes in this migration — those land in Slice 2.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # case_litigantes
    # ------------------------------------------------------------------
    op.create_table(
        "case_litigantes",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("cases.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("participante", sa.String(50), nullable=False),
        sa.Column("rut", sa.String(20), nullable=False),
        sa.Column("persona_type", sa.String(20), nullable=False),
        sa.Column("nombre", sa.String(500), nullable=False),
        sa.Column("natural_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "case_id", "natural_key", name="uq_case_litigante_natural_key"
        ),
    )

    # ------------------------------------------------------------------
    # case_notificaciones
    # ------------------------------------------------------------------
    op.create_table(
        "case_notificaciones",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("cases.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("rol", sa.String(50), nullable=False),
        sa.Column("estado_notif", sa.String(100), nullable=False),
        sa.Column("tipo_notif", sa.String(100), nullable=False),
        sa.Column("fecha_tramite", sa.DateTime(), nullable=True),
        sa.Column("tipo_participante", sa.String(50), nullable=False),
        sa.Column("nombre", sa.String(500), nullable=False),
        sa.Column("tramite", sa.String(255), nullable=False),
        sa.Column("obs_fallida", sa.Text(), nullable=True),
        sa.Column("natural_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "case_id", "natural_key", name="uq_case_notificacion_natural_key"
        ),
    )

    # ------------------------------------------------------------------
    # case_escritos
    # ------------------------------------------------------------------
    op.create_table(
        "case_escritos",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("cases.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("fecha_ingreso", sa.DateTime(), nullable=True),
        sa.Column("tipo_escrito", sa.String(255), nullable=False),
        sa.Column("solicitante", sa.String(500), nullable=False),
        sa.Column("tiene_documento", sa.Boolean(), nullable=False),
        sa.Column("tiene_anexo", sa.Boolean(), nullable=False),
        sa.Column("doc_token", sa.String(1024), nullable=True),
        sa.Column("natural_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "case_id", "natural_key", name="uq_case_escrito_natural_key"
        ),
    )

    # ------------------------------------------------------------------
    # case_exhortos
    # ------------------------------------------------------------------
    op.create_table(
        "case_exhortos",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("cases.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("rol_origen", sa.String(50), nullable=False),
        sa.Column("tipo_exhorto", sa.String(100), nullable=False),
        sa.Column("rol_destino", sa.String(50), nullable=False),
        sa.Column("fecha_ordena", sa.DateTime(), nullable=True),
        sa.Column("fecha_ingreso", sa.DateTime(), nullable=True),
        sa.Column("tribunal_destino", sa.String(500), nullable=False),
        sa.Column("estado", sa.String(100), nullable=False),
        sa.Column("natural_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "case_id", "natural_key", name="uq_case_exhorto_natural_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("case_exhortos")
    op.drop_table("case_escritos")
    op.drop_table("case_notificaciones")
    op.drop_table("case_litigantes")
