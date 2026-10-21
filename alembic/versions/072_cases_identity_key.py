"""cases identity key: (lawyer_id, rol, court_id, procedure)

Revision ID: 072
Revises: 071

``uq_cases_lawyer_rol`` keyed a causa on (lawyer_id, rol). The same ROL in two
courts, or in two cuadernos of one court, are different causas (measured
2026-10-21: 943 ROLs live in more than one court), so that key could not
represent them and every dedup built on it had to pick one.

The new key is a SUPERSET of the old one: any data that satisfied
(lawyer_id, rol) satisfies it, so creating it cannot fail on existing rows.
``procedure`` is coalesced to '' so a missing cuaderno cannot defeat the key
(NULL != NULL in a plain unique constraint). It is created BEFORE the old
constraint is dropped so the table is never without a uniqueness guard.

Downgrade restores the old constraint. It FAILS (and the transaction rolls
back) if rows now exist that only the new key allows; that is the intended
behavior, since losing those rows to satisfy the old key would be data loss.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "072"
down_revision: Union[str, None] = "071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_cases_identity "
        "ON cases (lawyer_id, rol, court_id, (coalesce(procedure, '')))"
    )
    op.drop_constraint("uq_cases_lawyer_rol", "cases", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_cases_lawyer_rol", "cases", ["lawyer_id", "rol"])
    op.execute("DROP INDEX uq_cases_identity")
