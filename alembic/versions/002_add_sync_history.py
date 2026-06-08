"""Add sync_history table

Revision ID: 002
Revises: 001
Create Date: 2026-06-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sync_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lawyer_id', sa.Integer(), nullable=False),
        sa.Column('competencia', sa.String(length=20), nullable=False),
        sa.Column('year', sa.String(length=4), nullable=True),
        sa.Column('cases_found', sa.Integer(), default=0),
        sa.Column('cases_new', sa.Integer(), default=0),
        sa.Column('cases_updated', sa.Integer(), default=0),
        sa.Column('movements_new', sa.Integer(), default=0),
        sa.Column('alerts_created', sa.Integer(), default=0),
        sa.Column('status', sa.String(length=20), default='completed'),
        sa.Column('error_message', sa.String(length=1024), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('triggered_by', sa.String(length=20), default='manual'),
        sa.ForeignKeyConstraint(['lawyer_id'], ['lawyers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sync_history_id', 'sync_history', ['id'])
    op.create_index('ix_sync_history_lawyer_competencia', 'sync_history', ['lawyer_id', 'competencia'])
    
    # Add competencia column to cases table for filtering
    op.add_column('cases', sa.Column('competencia', sa.String(length=20), default='civil'))


def downgrade() -> None:
    op.drop_column('cases', 'competencia')
    op.drop_table('sync_history')
