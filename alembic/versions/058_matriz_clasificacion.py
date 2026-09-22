"""matriz de clasificación M1/M2/M3 — reference tables + Case columns

Slice 1: derive the firm's operating matriz (M1 Baja / M1 Alta / M2 / M3)
automatically from the PJUD data we already scrape, instead of relying on
manual entry in Sysgal. Adds three reference tables seeded from the
business-owned taxonomy (``app/data/matriz/*.csv``, see
``app.services.matriz_seed.seed_matriz``) plus the computed columns
persisted on ``cases``.

- ``matriz_clasificacion``: one row per (proc_simple, etapa) pair, matriz
  nullable (some etapas legitimately have none, e.g. CAUSA ARCHIVADA).
- ``matriz_tramite_override``: (proc_antiguo, etapa, nombre_tramite) rows
  that OVERRIDE the etapa-level matriz for a specific trámite.
- ``matriz_pjud_mapeo``: editable PJUD-stage -> matriz-etapa mapping the
  business tunes via the API without a deploy.

Revision ID: 058
Revises: 057
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "058"
down_revision: Union[str, None] = "057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "matriz_clasificacion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proc_simple", sa.String(length=120), nullable=False),
        sa.Column("proc_antiguo", sa.String(length=120), nullable=False),
        sa.Column("etapa", sa.String(length=120), nullable=False),
        sa.Column("etapa_padre", sa.String(length=120), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=True),
        sa.Column("condicion_rol", sa.String(length=40), nullable=True),
        sa.Column("matriz", sa.String(length=20), nullable=True),
        sa.Column("observaciones", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proc_simple", "etapa", name="uq_matriz_clasificacion_proc_simple_etapa"),
    )
    op.create_index("ix_matriz_clasificacion_id", "matriz_clasificacion", ["id"], unique=False)
    op.create_index("ix_matriz_clasificacion_proc_simple", "matriz_clasificacion", ["proc_simple"], unique=False)

    op.create_table(
        "matriz_tramite_override",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proc_antiguo", sa.String(length=120), nullable=False),
        sa.Column("etapa", sa.String(length=120), nullable=False),
        sa.Column("nombre_tramite", sa.String(length=255), nullable=False),
        sa.Column("matriz", sa.String(length=20), nullable=False),
        sa.Column("observaciones", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proc_antiguo", "etapa", "nombre_tramite",
            name="uq_matriz_tramite_override_key",
        ),
    )
    op.create_index("ix_matriz_tramite_override_id", "matriz_tramite_override", ["id"], unique=False)

    op.create_table(
        "matriz_pjud_mapeo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pjud_stage", sa.String(length=255), nullable=False),
        sa.Column("matriz_etapa", sa.String(length=120), nullable=False),
        sa.Column("nota", sa.Text(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matriz_pjud_mapeo_id", "matriz_pjud_mapeo", ["id"], unique=False)
    op.create_index("ix_matriz_pjud_mapeo_pjud_stage", "matriz_pjud_mapeo", ["pjud_stage"], unique=True)

    op.add_column("cases", sa.Column("matriz", sa.String(length=20), nullable=True))
    op.add_column("cases", sa.Column("matriz_etapa", sa.String(length=80), nullable=True))
    op.add_column("cases", sa.Column("matriz_origen", sa.String(length=30), nullable=True))
    op.add_column("cases", sa.Column("matriz_proc_simple", sa.String(length=80), nullable=True))
    op.add_column("cases", sa.Column("matriz_computed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_cases_matriz", "cases", ["matriz"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cases_matriz", table_name="cases")
    op.drop_column("cases", "matriz_computed_at")
    op.drop_column("cases", "matriz_proc_simple")
    op.drop_column("cases", "matriz_origen")
    op.drop_column("cases", "matriz_etapa")
    op.drop_column("cases", "matriz")

    op.drop_index("ix_matriz_pjud_mapeo_pjud_stage", table_name="matriz_pjud_mapeo")
    op.drop_index("ix_matriz_pjud_mapeo_id", table_name="matriz_pjud_mapeo")
    op.drop_table("matriz_pjud_mapeo")

    op.drop_index("ix_matriz_tramite_override_id", table_name="matriz_tramite_override")
    op.drop_table("matriz_tramite_override")

    op.drop_index("ix_matriz_clasificacion_proc_simple", table_name="matriz_clasificacion")
    op.drop_index("ix_matriz_clasificacion_id", table_name="matriz_clasificacion")
    op.drop_table("matriz_clasificacion")
