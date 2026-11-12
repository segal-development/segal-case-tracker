"""Escritos generados en Sysgal (GEDOC): enrichment of Sysgal's numbers with our own ids.

Nothing is stored and Sysgal's figures are never recomputed; we only add ``lawyer_id`` / ``case_id``.
"""

from datetime import date

from sqlalchemy.orm import Session

from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario

MAX_RANGE_DAYS = 182


class RangeError(ValueError):
    """Invalid desde/hasta range; the message is the user-facing text."""


def check_range(desde: date, hasta: date) -> None:
    if desde > hasta:
        raise RangeError("La fecha desde debe ser anterior a hasta.")
    if (hasta - desde).days > MAX_RANGE_DAYS:
        raise RangeError("El rango máximo es de 6 meses.")


def _lawyer_by_sysgal(db: Session) -> dict[int, int]:
    rows = (
        db.query(SysgalUsuario.sysgal_id, SysgalUsuario.lawyer_id)
        .filter(SysgalUsuario.lawyer_id.isnot(None))
        .all()
    )
    return dict(rows)


def enrich_stats(db: Session, stats: dict) -> dict:
    """Stats as received, each ``por_abogado`` row with ``lawyer_id`` (null when unmapped)."""
    lawyers = _lawyer_by_sysgal(db)
    rows = [
        {**row, "lawyer_id": lawyers.get(row.get("abogado_id"))}
        for row in stats.get("por_abogado", [])
        if isinstance(row, dict)
    ]
    return {**stats, "por_abogado": rows}


def enrich_page(db: Session, page: dict) -> dict:
    """A page as received with ``lawyer_id`` and ``case_id`` per row (one causa query per page)."""
    items = [row for row in page.get("data", []) if isinstance(row, dict)]
    ids = {row["id_causa"] for row in items if row.get("id_causa") is not None}
    case_by_causa: dict[int, int] = {}
    if ids:
        case_by_causa = dict(
            db.query(SysgalCausa.sysgal_id, SysgalCausa.case_id)
            .filter(SysgalCausa.sysgal_id.in_(ids), SysgalCausa.case_id.isnot(None))
            .all()
        )
    lawyers = _lawyer_by_sysgal(db)
    data = [
        {**row, "lawyer_id": lawyers.get(row.get("abogado_id")), "case_id": case_by_causa.get(row.get("id_causa"))}
        for row in items
    ]
    return {**page, "data": data}
