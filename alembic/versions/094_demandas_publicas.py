"""demandas publicas: causas sin abogado demandado, búsqueda por fecha runs, public link tokens

Revision ID: 094
Revises: 093
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "094"
down_revision: Union[str, None] = "093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pjud_demandas_publicas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rol", sa.String(length=64), nullable=False),
        sa.Column("tribunal", sa.String(length=255), nullable=False),
        sa.Column("court_id", sa.Integer(), sa.ForeignKey("courts.id"), nullable=True),
        sa.Column("corte", sa.String(length=255), nullable=True),
        sa.Column("caratulado", sa.Text(), nullable=False, server_default=""),
        sa.Column("fecha_ingreso", sa.Date(), nullable=True),
        sa.Column("texto_demanda", sa.Text(), nullable=True),
        sa.Column("litigantes", sa.JSON(), nullable=False),
        sa.Column("encontrada_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("rol", "tribunal", name="uq_pjud_demandas_publicas_rol_tribunal"),
    )
    op.create_index("ix_pjud_demandas_publicas_id", "pjud_demandas_publicas", ["id"])
    op.create_index("ix_pjud_demandas_publicas_fecha_ingreso", "pjud_demandas_publicas", ["fecha_ingreso"])

    op.create_table(
        "pjud_busqueda_fecha_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("corte", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("contadores", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("fecha", "corte", name="uq_pjud_busqueda_fecha_runs_fecha_corte"),
    )
    op.create_index("ix_pjud_busqueda_fecha_runs_id", "pjud_busqueda_fecha_runs", ["id"])

    op.create_table(
        "public_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_public_link_tokens_id", "public_link_tokens", ["id"])
    op.create_index("ix_public_link_tokens_purpose", "public_link_tokens", ["purpose"])
    op.create_index("ix_public_link_tokens_token_hash", "public_link_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_public_link_tokens_token_hash", table_name="public_link_tokens")
    op.drop_index("ix_public_link_tokens_purpose", table_name="public_link_tokens")
    op.drop_index("ix_public_link_tokens_id", table_name="public_link_tokens")
    op.drop_table("public_link_tokens")
    op.drop_index("ix_pjud_busqueda_fecha_runs_id", table_name="pjud_busqueda_fecha_runs")
    op.drop_table("pjud_busqueda_fecha_runs")
    op.drop_index("ix_pjud_demandas_publicas_fecha_ingreso", table_name="pjud_demandas_publicas")
    op.drop_index("ix_pjud_demandas_publicas_id", table_name="pjud_demandas_publicas")
    op.drop_table("pjud_demandas_publicas")
