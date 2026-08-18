"""add evaluaciones module (staff evaluation / evaluación de procuradores)

New self-administrable module: replicate the firm's Google Form that rates a
person on admin-configurable 1-5 criteria plus a free-text comment. Creates four
tables and seeds the form's initial criteria so the module works out of the box.
Evaluables (who can be evaluated) are curated by the admin and left empty here.

Additive and isolated — touches no existing table.

Revision ID: 047
Revises: 046
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Initial criteria seeded so the form renders out of the box.
# (label, grupo, orden, permite_na)
_SEED_CRITERIOS = [
    ("Iniciativa", "Criterios", 0, False),
    ("Calidad en gestión con cliente", "Criterios", 1, False),
    ("Limpieza de Mensajería WhatsApp", "Criterios", 2, False),
    ("Conocimiento y habilidad técnica", "Criterios", 3, False),
    ("Atención de agenda", "Adicionales", 0, True),
    ("Revisión de estado diario", "Adicionales", 1, True),
    ("Atención de mensajería de otras áreas", "Adicionales", 2, True),
]


def upgrade() -> None:
    op.create_table(
        "evaluacion_criterios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("grupo", sa.String(length=100), nullable=False, server_default="Criterios"),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permite_na", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_evaluacion_criterios_id"), "evaluacion_criterios", ["id"])

    op.create_table(
        "evaluacion_evaluables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "lawyer_id",
            sa.Integer(),
            sa.ForeignKey("lawyers.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_evaluacion_evaluables_id"), "evaluacion_evaluables", ["id"])
    op.create_index(
        op.f("ix_evaluacion_evaluables_lawyer_id"), "evaluacion_evaluables", ["lawyer_id"]
    )

    op.create_table(
        "evaluaciones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "evaluado_lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=False
        ),
        sa.Column("evaluador_email", sa.String(length=255), nullable=False),
        sa.Column("comentarios", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(op.f("ix_evaluaciones_id"), "evaluaciones", ["id"])
    op.create_index(
        op.f("ix_evaluaciones_evaluado_lawyer_id"), "evaluaciones", ["evaluado_lawyer_id"]
    )
    op.create_index(
        op.f("ix_evaluaciones_evaluador_email"), "evaluaciones", ["evaluador_email"]
    )

    op.create_table(
        "evaluacion_respuestas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "evaluacion_id",
            sa.Integer(),
            sa.ForeignKey("evaluaciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "criterio_id",
            sa.Integer(),
            sa.ForeignKey("evaluacion_criterios.id"),
            nullable=False,
        ),
        sa.Column("puntaje", sa.Integer(), nullable=True),
    )
    op.create_index(op.f("ix_evaluacion_respuestas_id"), "evaluacion_respuestas", ["id"])
    op.create_index(
        op.f("ix_evaluacion_respuestas_evaluacion_id"),
        "evaluacion_respuestas",
        ["evaluacion_id"],
    )
    op.create_index(
        op.f("ix_evaluacion_respuestas_criterio_id"),
        "evaluacion_respuestas",
        ["criterio_id"],
    )

    # Seed the initial criteria (idempotent: only when the table is empty).
    criterios = sa.table(
        "evaluacion_criterios",
        sa.column("label", sa.String),
        sa.column("grupo", sa.String),
        sa.column("orden", sa.Integer),
        sa.column("permite_na", sa.Boolean),
        sa.column("activo", sa.Boolean),
    )
    bind = op.get_bind()
    already = bind.execute(sa.text("SELECT COUNT(*) FROM evaluacion_criterios")).scalar()
    if not already:
        op.bulk_insert(
            criterios,
            [
                {
                    "label": label,
                    "grupo": grupo,
                    "orden": orden,
                    "permite_na": permite_na,
                    "activo": True,
                }
                for (label, grupo, orden, permite_na) in _SEED_CRITERIOS
            ],
        )


def downgrade() -> None:
    op.drop_table("evaluacion_respuestas")
    op.drop_table("evaluaciones")
    op.drop_table("evaluacion_evaluables")
    op.drop_table("evaluacion_criterios")
