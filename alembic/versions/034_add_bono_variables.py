"""add lawyers.nivel + bono_variables table (+ seed niveles, alta Sylvia)

Sistema de Hitos — Slice 2 (V1–V4 + liquidación). Adds the bonus tier to each
lawyer and the monthly manual-input table Dirección Jurídica fills. Seeds the
known niveles by name and creates Sylvia Manríquez (junior, sin credenciales
aún: inactive with a placeholder RUT until her real RUT/creds are provided).

Revision ID: 034
Revises: 033
"""
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Known bonus roster from the "SISTEMA DE HITOS" sheet. Matched by normalized
# name-token containment so accents/casing/extra surnames don't break it.
_NIVELES = {
    "junior": [
        ("sylvia", "manriquez"),
        ("fernanda", "arroyo"),
        ("benjamin", "hervas"),
        ("gonzalo", "calderon"),
        ("valentina", "araya"),
    ],
    "pleno": [
        ("sandy", "quijada"),
        ("eduardo", "venegas"),
        ("luis", "contreras"),
    ],
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


def upgrade() -> None:
    op.add_column("lawyers", sa.Column("nivel", sa.String(length=10), nullable=True))

    op.create_table(
        "bono_variables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False),
        sa.Column("periodo", sa.String(length=7), nullable=False),
        sa.Column("nivel", sa.String(length=10), nullable=False),
        sa.Column("clientes_m2", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clientes_activos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("causas_asignadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("causas_cumplidas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reclamos_leve", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reclamos_medio", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reclamos_grave", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("renovaciones", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verificado_dj", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by_rut", sa.String(length=20), nullable=True),
        sa.Column("updated_by_rut", sa.String(length=20), nullable=True),
        sa.Column("updated_by_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("lawyer_id", "periodo", name="uq_bono_variables_lawyer_periodo"),
    )
    op.create_index("ix_bono_variables_lawyer_id", "bono_variables", ["lawyer_id"])
    op.create_index("ix_bono_variables_periodo", "bono_variables", ["periodo"])

    # --- seed niveles by normalized-name match ---
    conn = op.get_bind()
    lawyers = conn.execute(sa.text("SELECT id, name FROM lawyers")).fetchall()
    norm_by_id = {row[0]: _norm(row[1]) for row in lawyers}

    seen_junior_sylvia = False
    for nivel, roster in _NIVELES.items():
        for first, last in roster:
            match_id = next(
                (lid for lid, nm in norm_by_id.items() if first in nm and last in nm),
                None,
            )
            if match_id is not None:
                conn.execute(
                    sa.text("UPDATE lawyers SET nivel = :n WHERE id = :id"),
                    {"n": nivel, "id": match_id},
                )
                if (first, last) == ("sylvia", "manriquez"):
                    seen_junior_sylvia = True

    # Sylvia has no system account yet — create her (inactive, placeholder RUT)
    # so she is in the bonus roster; her real RUT/credentials come later.
    if not seen_junior_sylvia:
        conn.execute(
            sa.text(
                "INSERT INTO lawyers (rut, name, role, is_active, is_firm_lawyer, nivel) "
                "VALUES (:rut, :name, 'lawyer', false, true, 'junior')"
            ),
            {"rut": "PEND-SYLVIA", "name": "Sylvia Manríquez"},
        )


def downgrade() -> None:
    op.drop_table("bono_variables")
    op.drop_column("lawyers", "nivel")
