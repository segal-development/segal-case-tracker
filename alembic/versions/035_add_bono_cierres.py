"""add bono_cierres table (monthly bonus period close / payroll freeze)

Sistema de Hitos — Slice 2c. Lets Dirección Jurídica close a bonus período so
its variables and hitos become read-only (a paid liquidación can't change after
the fact); an admin can reopen (audited).

Revision ID: 035
Revises: 034
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bono_cierres",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("periodo", sa.String(length=7), nullable=False),
        sa.Column("estado", sa.String(length=10), nullable=False, server_default="cerrado"),
        sa.Column("cerrado_by_rut", sa.String(length=20), nullable=True),
        sa.Column("cerrado_by_name", sa.String(length=255), nullable=True),
        sa.Column("cerrado_at", sa.DateTime(), nullable=True),
        sa.Column("reabierto_by_rut", sa.String(length=20), nullable=True),
        sa.Column("reabierto_by_name", sa.String(length=255), nullable=True),
        sa.Column("reabierto_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("periodo", name="uq_bono_cierres_periodo"),
    )
    op.create_index("ix_bono_cierres_periodo", "bono_cierres", ["periodo"])


def downgrade() -> None:
    op.drop_table("bono_cierres")
