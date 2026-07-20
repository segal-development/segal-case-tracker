"""add is_firm_lawyer flag to lawyers

Marks which accounts are the firm's OWN litigating lawyers, so the transversal
study views (risk board, productividad) count only their caseload — not the
~475 opposing/external abogados that appear as case litigantes. New accounts
default in (True); the non-litigating accounts (super-admin, auditor) are set out.

Revision ID: 031
Revises: 030
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lawyers",
        sa.Column(
            "is_firm_lawyer",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    # Exclude the non-litigating accounts: the auditor role and the super-admin
    # seed account. Every real lawyer (incl. the admin who also litigates) stays in.
    op.execute(
        "UPDATE lawyers SET is_firm_lawyer = false "
        "WHERE role = 'auditor' OR rut = 'mtoro-admin'"
    )


def downgrade() -> None:
    op.drop_column("lawyers", "is_firm_lawyer")
