"""add presentaciones module (external Presentación / GEDOC escrito-upload API)

New external API module: GEDOC queues a filing (demanda/escrito) to be presented
at the PJUD's OJV. Slice 1 is the API contract + persistence ONLY — a created
presentación just sits at ``estado="en_cola"``; no browser automation, no PJUD
calls, no worker. Creates two tables: ``presentacion_api_keys`` (own isolated
auth, mirrors ``redaccion_api_keys``) and ``presentaciones`` (the queued
filings).

Additive and isolated — touches no existing table.

Revision ID: 049
Revises: 048
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "presentacion_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_presentacion_api_keys_id"), "presentacion_api_keys", ["id"])
    op.create_index(
        op.f("ix_presentacion_api_keys_key_hash"),
        "presentacion_api_keys",
        ["key_hash"],
        unique=True,
    )

    op.create_table(
        "presentaciones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "api_key_id",
            sa.Integer(),
            sa.ForeignKey("presentacion_api_keys.id"),
            nullable=True,
        ),
        sa.Column("tipo_gestion", sa.String(length=50), nullable=False),
        sa.Column("modo", sa.String(length=20), nullable=False, server_default="semiauto"),
        sa.Column("competencia", sa.String(length=100), nullable=True),
        sa.Column("corte", sa.String(length=255), nullable=True),
        sa.Column("tribunal", sa.String(length=255), nullable=True),
        sa.Column("rol", sa.String(length=50), nullable=True),
        sa.Column("anio", sa.String(length=4), nullable=True),
        sa.Column("procedimiento", sa.String(length=255), nullable=True),
        sa.Column("materia", sa.String(length=255), nullable=True),
        sa.Column("dirigido_a_rut", sa.String(length=20), nullable=True),
        sa.Column("credential_ref", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("estado", sa.String(length=30), nullable=False, server_default="en_cola"),
        sa.Column("rol_asignado", sa.String(length=50), nullable=True),
        sa.Column("ruc", sa.String(length=50), nullable=True),
        sa.Column("numero_identificador", sa.String(length=50), nullable=True),
        sa.Column("fecha_envio", sa.DateTime(), nullable=True),
        sa.Column("certificado_url", sa.String(length=1024), nullable=True),
        sa.Column("error_detail", sa.String(length=1024), nullable=True),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_presentaciones_id"), "presentaciones", ["id"])
    op.create_index(
        op.f("ix_presentaciones_idempotency_key"),
        "presentaciones",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(op.f("ix_presentaciones_api_key_id"), "presentaciones", ["api_key_id"])
    op.create_index(op.f("ix_presentaciones_estado"), "presentaciones", ["estado"])


def downgrade() -> None:
    op.drop_table("presentaciones")
    op.drop_table("presentacion_api_keys")
