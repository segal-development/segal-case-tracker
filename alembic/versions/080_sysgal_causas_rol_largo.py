"""sysgal_causas: room for free-text ROLs

The first real sync (2026-11-09) failed on a 60-character ROL: Sysgal stores free text in that
field for about half of its 110,064 causas ("DEMANDA CUIDADO PERSONAL Y ..."). rol and rol_norm
go to 255; rut_cliente to 30 as headroom (real max 9).

Revision ID: 080
Revises: 079
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "080"
down_revision: Union[str, None] = "079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sysgal_causas") as batch:
        batch.alter_column("rol", type_=sa.String(255), existing_type=sa.String(50), existing_nullable=False)
        batch.alter_column("rol_norm", type_=sa.String(255), existing_type=sa.String(50), existing_nullable=False)
        batch.alter_column("rut_cliente", type_=sa.String(30), existing_type=sa.String(20), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("sysgal_causas") as batch:
        batch.alter_column("rut_cliente", type_=sa.String(20), existing_type=sa.String(30), existing_nullable=True)
        batch.alter_column("rol_norm", type_=sa.String(50), existing_type=sa.String(255), existing_nullable=False)
        batch.alter_column("rol", type_=sa.String(50), existing_type=sa.String(255), existing_nullable=False)
