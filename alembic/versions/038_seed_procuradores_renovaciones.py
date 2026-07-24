"""seed procuradores for the renovaciones selector

Carla asked to add 8 procuradores to the renovación 'abogado' selector. They are
NOT litigating lawyers, so they are seeded with role='procurador' and
is_firm_lawyer=False — this keeps them OUT of the firm-wide stats/risk-board and
the Hitos/Bono selectors (which filter is_firm_lawyer), while the Renovaciones
endpoints explicitly opt this role in. They never log in (no password_hash).

Revision ID: 038
Revises: 037
"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (normalized RUT, display name) — names in the firm's "NOMBRES APELLIDOS" style.
PROCURADORES = [
    ("20613995-1", "CAMILA ANDREA CANALES COÑUENAO"),
    ("19557032-9", "CONSTANZA ANDREA CARO CORTEZ"),
    ("16346743-7", "PABLO DIEGO FLORES MÉNDEZ"),
    ("19800771-4", "CAMILA JAVIERA MONREAL LEPE"),
    ("20049612-4", "JAVIERA LISSETTE MONTANARES VENEGAS"),
    ("20397791-3", "MAURICIO ALEJANDRO PEÑA ESCALANTE"),
    ("20568122-1", "MARÍA JOSÉ VERA PICHUNANTE"),
    ("20117653-0", "PATRICIA ALEJANDRA ZÚÑIGA YANTÉN"),
]


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
                "rut": rut,
                "name": name,
                "role": "procurador",
                "is_firm_lawyer": False,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            for rut, name in PROCURADORES
        ],
    )


def downgrade() -> None:
    ruts = tuple(rut for rut, _ in PROCURADORES)
    op.execute(
        sa.text("DELETE FROM lawyers WHERE role = 'procurador' AND rut IN :ruts").bindparams(
            sa.bindparam("ruts", value=ruts, expanding=True)
        )
    )
