"""add hitos (milestone -> bonus) tables + seed the hito-type catalog

Slice 1 of the SISTEMA DE HITOS: a hito-type catalog (values pre-loaded from the
firm's PARÁMETROS) and a hitos table (entered with mandatory PJUD evidence,
approved by an admin).

Revision ID: 032
Revises: 031
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Catalog seeded from the "PARÁMETROS" sheet (gross CLP values).
_HITO_TIPOS = [
    ("junior_h1_conversion", "Conversión preventiva → M1 Alta", "junior", 2423,
     "Escrito presentado · M1 Alta", "Captura PJUD del escrito presentado", 1),
    ("pleno_excepcion_dilatoria", "Excepción dilatoria acogida", "pleno", 808,
     "INGRESO EXCEPCIONES DILATORIAS", "Sentencia/Fallo favorable al deudor", 2),
    ("pleno_exhibicion", "Exhibición terminada", "pleno", 2423,
     "EXHIBICIÓN DOCUMENTOS", "Resolución tribunal cierre exhibición", 3),
    ("pleno_incidente_6m", "Defensa exitosa incidente ≥6m", "pleno", 5654,
     "INGRESO INCIDENTE", "Resolución + verificación continuidad", 4),
    ("pleno_abandono_6m", "Abandono 6 meses fallo favorable", "pleno", 8077,
     "INGRESO ABANDONO 6 MESES", "Fallo favorable firme + certificación", 5),
    ("pleno_abandono_3a", "Abandono 3 años fallo favorable", "pleno", 8077,
     "INGRESO ABANDONO 3 AÑOS", "Fallo favorable firme + certificación", 6),
    ("pleno_prescripcion", "Prescripción terminada (sentencia firme)", "pleno", 8077,
     "INGRESO EXCEPCIONES PRESCRIPCIÓN", "Sentencia firme y ejecutoriada", 7),
]


def upgrade() -> None:
    op.create_table(
        "hito_tipos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("nivel", sa.String(length=10), nullable=False),
        sa.Column("valor_bruto", sa.Integer(), nullable=False),
        sa.Column("etapa_tramite", sa.String(length=255), nullable=True),
        sa.Column("verificacion", sa.String(length=500), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_hito_tipos_code", "hito_tipos", ["code"], unique=True)

    op.create_table(
        "hitos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("hito_tipo_id", sa.Integer(), sa.ForeignKey("hito_tipos.id"), nullable=False),
        sa.Column("valor_bruto", sa.Integer(), nullable=False),
        sa.Column("fecha_hito", sa.Date(), nullable=False),
        sa.Column("rol_causa", sa.String(length=50), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=True),
        sa.Column("procedimiento", sa.String(length=100), nullable=True),
        sa.Column("descripcion", sa.String(length=500), nullable=True),
        sa.Column("etapa_sysgal", sa.String(length=100), nullable=True),
        sa.Column("tramite_sysgal", sa.String(length=255), nullable=True),
        sa.Column("evidencia_storage_key", sa.String(length=512), nullable=True),
        sa.Column("evidencia_filename", sa.String(length=255), nullable=True),
        sa.Column("evidencia_content_type", sa.String(length=100), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("created_by_rut", sa.String(length=20), nullable=True),
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
        sa.Column("aprobado_by_rut", sa.String(length=20), nullable=True),
        sa.Column("aprobado_by_name", sa.String(length=255), nullable=True),
        sa.Column("aprobado_at", sa.DateTime(), nullable=True),
        sa.Column("rechazo_motivo", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_hitos_lawyer_id", "hitos", ["lawyer_id"])
    op.create_index("ix_hitos_hito_tipo_id", "hitos", ["hito_tipo_id"])
    op.create_index("ix_hitos_case_id", "hitos", ["case_id"])
    op.create_index("ix_hitos_fecha_hito", "hitos", ["fecha_hito"])

    tipos = sa.table(
        "hito_tipos",
        sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("nivel", sa.String), sa.column("valor_bruto", sa.Integer),
        sa.column("etapa_tramite", sa.String), sa.column("verificacion", sa.String),
        sa.column("orden", sa.Integer),
    )
    op.bulk_insert(tipos, [
        {"code": c, "label": la, "nivel": n, "valor_bruto": v,
         "etapa_tramite": et, "verificacion": ve, "orden": o}
        for (c, la, n, v, et, ve, o) in _HITO_TIPOS
    ])


def downgrade() -> None:
    op.drop_table("hitos")
    op.drop_table("hito_tipos")
