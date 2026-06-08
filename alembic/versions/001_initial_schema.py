"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Lawyers table
    op.create_table(
        'lawyers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rut', sa.String(length=12), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('encrypted_pjud_password', sa.String(length=512), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True, default=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lawyers_id', 'lawyers', ['id'])
    op.create_index('ix_lawyers_rut', 'lawyers', ['rut'], unique=True)
    op.create_index('ix_lawyers_email', 'lawyers', ['email'], unique=True)

    # Courts table
    op.create_table(
        'courts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('region', sa.String(length=50), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('pjud_competencia', sa.Integer(), nullable=True),
        sa.Column('pjud_tribunal', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_courts_id', 'courts', ['id'])
    op.create_index('ix_courts_code', 'courts', ['code'], unique=True)

    # Cases table
    op.create_table(
        'cases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lawyer_id', sa.Integer(), nullable=False),
        sa.Column('court_id', sa.Integer(), nullable=False),
        sa.Column('rol', sa.String(length=50), nullable=False),
        sa.Column('rit', sa.String(length=50), nullable=True),
        sa.Column('plaintiff', sa.String(length=500), nullable=True),
        sa.Column('defendant', sa.String(length=500), nullable=True),
        sa.Column('matter', sa.String(length=255), nullable=True),
        sa.Column('procedure', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True, default='active'),
        sa.Column('pjud_causa_id', sa.String(length=100), nullable=True),
        sa.Column('filed_at', sa.DateTime(), nullable=True),
        sa.Column('last_movement_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['lawyer_id'], ['lawyers.id']),
        sa.ForeignKeyConstraint(['court_id'], ['courts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cases_id', 'cases', ['id'])
    op.create_index('ix_cases_rol', 'cases', ['rol'])

    # Movements table
    op.create_table(
        'movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('stage', sa.String(length=255), nullable=True),
        sa.Column('procedure', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('folio', sa.String(length=50), nullable=True),
        sa.Column('document_url', sa.String(length=1024), nullable=True),
        sa.Column('pjud_movement_id', sa.String(length=100), nullable=True),
        sa.Column('movement_date', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_movements_id', 'movements', ['id'])

    # Documents table
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('movement_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('gcs_path', sa.String(length=512), nullable=True),
        sa.Column('pjud_url', sa.String(length=1024), nullable=True),
        sa.Column('document_date', sa.DateTime(), nullable=True),
        sa.Column('downloaded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id']),
        sa.ForeignKeyConstraint(['movement_id'], ['movements.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_documents_id', 'documents', ['id'])

    # Alerts table
    op.create_table(
        'alerts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lawyer_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('movement_id', sa.Integer(), nullable=True),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('email_sent', sa.Boolean(), nullable=True, default=False),
        sa.Column('email_sent_at', sa.DateTime(), nullable=True),
        sa.Column('webhook_sent', sa.Boolean(), nullable=True, default=False),
        sa.Column('webhook_sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['lawyer_id'], ['lawyers.id']),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id']),
        sa.ForeignKeyConstraint(['movement_id'], ['movements.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_alerts_id', 'alerts', ['id'])

    # Webhooks table
    op.create_table(
        'webhooks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lawyer_id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(length=1024), nullable=False),
        sa.Column('events', sa.JSON(), nullable=False),
        sa.Column('secret', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True, default=True),
        sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=True, default=0),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['lawyer_id'], ['lawyers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_webhooks_id', 'webhooks', ['id'])

    # Audit logs table
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lawyer_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('resource_type', sa.String(length=50), nullable=True),
        sa.Column('resource_id', sa.Integer(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['lawyer_id'], ['lawyers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_logs_id', 'audit_logs', ['id'])


def downgrade() -> None:
    op.drop_table('audit_logs')
    op.drop_table('webhooks')
    op.drop_table('alerts')
    op.drop_table('documents')
    op.drop_table('movements')
    op.drop_table('cases')
    op.drop_table('courts')
    op.drop_table('lawyers')
