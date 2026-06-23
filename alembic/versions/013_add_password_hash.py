"""Add password_hash column to lawyers.

Revision ID: 013
Revises: 012
Create Date: 2026-06-23

Adds bcrypt password_hash to support app-level email+password authentication
(as opposed to the existing PJUD/Clave Única flows).  The column is nullable
so existing records are unaffected until a password is explicitly set.

Changes:
- lawyers: add password_hash VARCHAR(255) NULL
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lawyers", sa.Column("password_hash", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("lawyers", "password_hash")
