"""sysgal_incorporaciones: queue of Sysgal causas not yet followed by our system

Revision ID: 081
Revises: 080
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "081"
down_revision: Union[str, None] = "080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sysgal_incorporaciones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sysgal_causa_id", sa.Integer(), sa.ForeignKey("sysgal_causas.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default=sa.text("'pendiente'")),
        sa.Column("prioridad", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ultimo_error", sa.String(255), nullable=True),
        sa.Column("proximo_intento_at", sa.DateTime(), nullable=True),
        sa.Column("corte", sa.Integer(), nullable=True),
        sa.Column("court_id", sa.Integer(), sa.ForeignKey("courts.id"), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("sysgal_causa_id", name="uq_sysgal_incorporaciones_causa"),
    )
    op.create_index("ix_sysgal_incorporaciones_id", "sysgal_incorporaciones", ["id"])
    op.create_index("ix_sysgal_incorporaciones_estado", "sysgal_incorporaciones", ["estado"])
    op.create_index("ix_sysgal_incorporaciones_case_id", "sysgal_incorporaciones", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_sysgal_incorporaciones_case_id", table_name="sysgal_incorporaciones")
    op.drop_index("ix_sysgal_incorporaciones_estado", table_name="sysgal_incorporaciones")
    op.drop_index("ix_sysgal_incorporaciones_id", table_name="sysgal_incorporaciones")
    op.drop_table("sysgal_incorporaciones")
