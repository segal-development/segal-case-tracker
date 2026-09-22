"""add hito_form_links (generic public hito-form links, e.g. procuradores)

One row per ``kind`` holding a shared secret token. First kind:
``procuradores`` — the firm's assistants open one shared link, pick the lawyer
they work for and submit a hito on their behalf. ``token`` NULL = revoked.

Revision ID: 057
Revises: 056
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "057"
down_revision: Union[str, None] = "056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hito_form_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hito_form_links_id", "hito_form_links", ["id"], unique=False)
    op.create_index("ix_hito_form_links_kind", "hito_form_links", ["kind"], unique=True)
    op.create_index("ix_hito_form_links_token", "hito_form_links", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_hito_form_links_token", table_name="hito_form_links")
    op.drop_index("ix_hito_form_links_kind", table_name="hito_form_links")
    op.drop_index("ix_hito_form_links_id", table_name="hito_form_links")
    op.drop_table("hito_form_links")
