"""Helpers to check whether a bonus período is closed (payroll frozen).

Shared by the bono endpoints (block variable edits) and the hitos endpoints
(block creating/approving hitos that fall in a closed month), so a closed
liquidación can never change after the fact.
"""
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models.bono_cierre import BonoCierre, CIERRE_CERRADO


def periodo_de_fecha(d: date) -> str:
    """The 'YYYY-MM' period a date belongs to."""
    return f"{d.year:04d}-{d.month:02d}"


def is_cerrado(db: Session, periodo: str) -> bool:
    """True if the given 'YYYY-MM' period is closed."""
    row = (
        db.query(BonoCierre)
        .filter(BonoCierre.periodo == periodo, BonoCierre.estado == CIERRE_CERRADO)
        .first()
    )
    return row is not None


def get_cierre(db: Session, periodo: str) -> Optional[BonoCierre]:
    """The cierre row for a period, if any (closed or reopened)."""
    return db.query(BonoCierre).filter(BonoCierre.periodo == periodo).first()
