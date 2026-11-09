"""sysgal_causas and sysgal_usuarios: responsible lawyer per case, as reported by Sysgal

Revision ID: 079
Revises: 078

Additive: two new tables. ``sysgal_causas.case_id`` is the match to our cases (SET NULL on
delete); ``sysgal_usuarios.lawyer_id`` is the human-confirmed mapping to our lawyers.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "079"
down_revision: Union[str, None] = "078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sysgal_usuarios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sysgal_id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("perfil", sa.String(100), nullable=True),
        sa.Column("estado", sa.String(50), nullable=True),
        sa.Column("correo", sa.String(255), nullable=True),
        sa.Column("lawyer_id", sa.Integer(), sa.ForeignKey("lawyers.id"), nullable=True),
        sa.Column("mapped_by", sa.String(20), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sysgal_usuarios_id", "sysgal_usuarios", ["id"])
    op.create_index("ix_sysgal_usuarios_sysgal_id", "sysgal_usuarios", ["sysgal_id"], unique=True)
    op.create_index("ix_sysgal_usuarios_lawyer_id", "sysgal_usuarios", ["lawyer_id"])

    op.create_table(
        "sysgal_causas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sysgal_id", sa.Integer(), nullable=False),
        sa.Column("rol", sa.String(50), nullable=False),
        sa.Column("rol_norm", sa.String(50), nullable=False),
        sa.Column("tribunal", sa.String(255), nullable=False),
        sa.Column("tribunal_codigo", sa.String(20), nullable=True),
        sa.Column("rut_cliente", sa.String(20), nullable=True),
        sa.Column("abogado_sysgal_id", sa.Integer(), nullable=True),
        sa.Column("abogado_nombre", sa.String(255), nullable=True),
        sa.Column("etapa", sa.String(255), nullable=True),
        sa.Column("tramite", sa.String(255), nullable=True),
        sa.Column("estado", sa.String(50), nullable=True),
        sa.Column("competencia", sa.String(50), nullable=True),
        sa.Column("eliminada", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("responsable_desde", sa.Date(), nullable=True),
        sa.Column("sysgal_updated_at", sa.DateTime(), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("match_method", sa.String(30), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sysgal_causas_id", "sysgal_causas", ["id"])
    op.create_index("ix_sysgal_causas_sysgal_id", "sysgal_causas", ["sysgal_id"], unique=True)
    op.create_index("ix_sysgal_causas_rol_norm", "sysgal_causas", ["rol_norm"])
    op.create_index("ix_sysgal_causas_abogado_sysgal_id", "sysgal_causas", ["abogado_sysgal_id"])
    op.create_index("ix_sysgal_causas_case_id", "sysgal_causas", ["case_id"])


def downgrade() -> None:
    for ix in ("case_id", "abogado_sysgal_id", "rol_norm", "sysgal_id", "id"):
        op.drop_index(f"ix_sysgal_causas_{ix}", table_name="sysgal_causas")
    op.drop_table("sysgal_causas")
    for ix in ("lawyer_id", "sysgal_id", "id"):
        op.drop_index(f"ix_sysgal_usuarios_{ix}", table_name="sysgal_usuarios")
    op.drop_table("sysgal_usuarios")
