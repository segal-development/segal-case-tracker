"""add lawyers.hito_form_token (public hito form link)

Lawyers have no app login, so they submit their hitos through a PUBLIC form
reached by a per-lawyer secret link. The link carries this token
(``secrets.token_urlsafe(32)``); the admin generates, regenerates or revokes
it from the Hitos screen. NULL = no link issued.

Revision ID: 056
Revises: 055
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: Union[str, None] = "055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lawyers", sa.Column("hito_form_token", sa.String(length=64), nullable=True))
    op.create_index("ix_lawyers_hito_form_token", "lawyers", ["hito_form_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_lawyers_hito_form_token", table_name="lawyers")
    op.drop_column("lawyers", "hito_form_token")
