"""backfill renovaciones.lawyer_id for procurador usernames

The renovaciones importer matched each raw renovador ('MVERA', 'PACEVEDO', ...)
to a lawyer by an 'INITIAL+SURNAME' index, but that index only included firm
lawyers — procuradores (is_firm_lawyer=False) were excluded, so THEIR usernames
never matched and their renovaciones landed with lawyer_id=NULL + a raw name.

The code fix now indexes procuradores too. This migration backfills the rows
already imported: for every renovación with lawyer_id IS NULL, it recomputes the
same INITIAL+SURNAME match (firm lawyers first so they win collisions) over firm
lawyers + procuradores, and fills lawyer_id when it resolves. E.g. 'MVERA' →
María José Vera Pichunante, 'CCARO' → Constanza Caro, 'JMONTANARES' → Javiera
Montanares. Non-destructive: only fills NULLs, never overwrites an existing id.

Revision ID: 043
Revises: 042
"""
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _norm(s) -> str:
    return (
        unicodedata.normalize("NFKD", str(s or ""))
        .encode("ascii", "ignore")
        .decode()
        .upper()
        .strip()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Firm lawyers first (is_firm_lawyer DESC) so they win any username collision.
    lawyers = conn.execute(
        sa.text(
            "SELECT id, name FROM lawyers "
            "WHERE is_firm_lawyer = true OR role = 'procurador' "
            "ORDER BY is_firm_lawyer DESC"
        )
    ).fetchall()
    idx: dict[str, int] = {}
    for lid, name in lawyers:
        toks = _norm(name).split()
        if not toks:
            continue
        ini = toks[0][0]
        for tok in toks[1:]:
            idx.setdefault(ini + tok, lid)

    rows = conn.execute(
        sa.text(
            "SELECT id, renovador_raw FROM renovaciones "
            "WHERE lawyer_id IS NULL AND renovador_raw IS NOT NULL"
        )
    ).fetchall()
    fixed = 0
    for rid, raw in rows:
        lid = idx.get(_norm(raw))
        if lid is not None:
            conn.execute(
                sa.text("UPDATE renovaciones SET lawyer_id = :lid WHERE id = :rid"),
                {"lid": lid, "rid": rid},
            )
            fixed += 1
    print(f"[043] backfilled lawyer_id on {fixed}/{len(rows)} unmatched renovaciones")


def downgrade() -> None:
    # Non-reversible: we cannot tell which lawyer_ids were backfilled here vs set
    # at import time. No-op on purpose (the forward fill is safe to keep).
    pass
