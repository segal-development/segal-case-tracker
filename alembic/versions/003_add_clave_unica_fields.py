"""Add Clave Unica authentication fields to lawyers table

Revision ID: 003
Revises: 002
Create Date: 2026-06-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add Clave Unica fields to lawyers table
    op.add_column(
        'lawyers',
        sa.Column('clave_unica_rut', sa.String(length=12), nullable=True)
    )
    op.add_column(
        'lawyers',
        sa.Column('encrypted_clave_unica_password', sa.String(length=512), nullable=True)
    )
    op.add_column(
        'lawyers',
        sa.Column('preferred_auth_method', sa.String(length=20), server_default='captcha')
    )


def downgrade() -> None:
    op.drop_column('lawyers', 'preferred_auth_method')
    op.drop_column('lawyers', 'encrypted_clave_unica_password')
    op.drop_column('lawyers', 'clave_unica_rut')
