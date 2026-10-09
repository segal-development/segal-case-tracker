"""add Hugo Maureira so he can file hitos through the public form

Marcelo asked for this lawyer to be added only so he can register hitos
through the public form (/registrar-hito/:token) -- not to have his PJUD
portfolio scraped.

``is_firm_lawyer`` is nevertheless True, and it HAS to be: the public form
resolves its token with ``Lawyer.is_firm_lawyer.is_(True)``
(app/api/v1/hitos.py, the ``/public/...`` lookup), so a lawyer seeded with
False would get a link that 404s. There is no "hitos only" shape. The
consequence is deliberate and was agreed: he therefore also appears in the
Hitos and Bono selectors and in the firm-wide stats, which is what you want
for someone whose hitos are going to be paid.

What he does NOT get, and what separates him from a full litigating lawyer:
no PJUD credentials, so the sync rotation never touches him and he brings no
causas; and no ``password_hash``, so he cannot log in -- same as every other
abogado in this firm (only Carla, admins and auditors use the app).

The form link itself is NOT created here. It is generated from the Hitos
screen (POST /hitos/form-links/{lawyer_id}), which already exists, so the
token stays out of version control.

RUT check digit verified: 20488059-K.

Revision ID: 065
Revises: 064
"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "065"
down_revision: Union[str, None] = "064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Normalized RUT, and the name in the firm's "NOMBRES APELLIDOS" style
# (given as "MAUREIRA VÉLIZ, HUGO ALEJANDRO").
RUT = "20488059-K"
NAME = "HUGO ALEJANDRO MAUREIRA VÉLIZ"


def upgrade() -> None:
    lawyers = sa.table(
        "lawyers",
        sa.column("rut", sa.String),
        sa.column("name", sa.String),
        sa.column("role", sa.String),
        sa.column("is_firm_lawyer", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        lawyers,
        [
            {
                "rut": RUT,
                "name": NAME,
                "role": "lawyer",
                "is_firm_lawyer": True,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM lawyers WHERE rut = :rut").bindparams(sa.bindparam("rut", value=RUT))
    )
