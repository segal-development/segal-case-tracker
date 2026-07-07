"""add credential_alert_sent_at to lawyers

Revision ID: 025
Revises: 024
Create Date: 2026-07-06

Adds a nullable timestamp column to lawyers used to de-dup supervisor
credential-change alert emails: one alert per credential-failure episode.
Set when the alert is sent; cleared (NULL) the next time that lawyer's PJUD
login succeeds, so a future credential change alerts again.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lawyers",
        sa.Column("credential_alert_sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lawyers", "credential_alert_sent_at")
