"""app_text_settings: key/text settings (the egress allow-list lives here)

Revision ID: 093
Revises: 092
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "093"
down_revision: Union[str, None] = "092"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_text_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_app_text_settings_id", "app_text_settings", ["id"])
    op.create_index("ix_app_text_settings_key", "app_text_settings", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_text_settings_key", table_name="app_text_settings")
    op.drop_index("ix_app_text_settings_id", table_name="app_text_settings")
    op.drop_table("app_text_settings")
