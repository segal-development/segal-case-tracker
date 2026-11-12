"""Avance de cartera: monthly snapshot of Sysgal's cartera statistics.

Sysgal's numbers are stored as received and never recomputed. The bono V3 reads each
lawyer's ``cobertura`` from the snapshot of the período (see ``cobertura_by_lawyer``).
"""

import calendar
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.avance_cartera import AvanceCarteraSnapshot
from app.models.lawyer import Lawyer
from app.models.sysgal_usuario import SysgalUsuario
from app.services import bono_cierre_service as cierre_svc

logger = logging.getLogger(__name__)

# First período with a Sysgal photo. Earlier months always use the manual weeks for the bono V3.
FOTO_DESDE = "2026-11"


class PeriodoNoTerminado(Exception):
    """The month has not ended yet."""


class PeriodoSinFoto(Exception):
    """The período is before ``FOTO_DESDE``: no Sysgal photo exists for it."""


class PeriodoCerrado(Exception):
    """The bono liquidación of that período is closed."""


def cartera_lawyer_ids(db: Session) -> set[int]:
    """Ids of the active, non-admin lawyers mapped to a Sysgal user (one query).

    Criterion of ``en_cartera`` on the Avance de cartera screen; independent of the bono niveles.
    """
    rows = (
        db.query(Lawyer.id)
        .join(SysgalUsuario, SysgalUsuario.lawyer_id == Lawyer.id)
        .filter(Lawyer.is_active.is_(True), Lawyer.role != "admin")
        .all()
    )
    return {lid for (lid,) in rows}


def periodo_bounds(periodo: str) -> tuple[date, date]:
    """First and last day of ``AAAA-MM``; ``ValueError`` when malformed."""
    try:
        year_s, month_s = periodo.split("-")
        if len(year_s) != 4 or len(month_s) != 2:
            raise ValueError
        year, month = int(year_s), int(month_s)
        first = date(year, month, 1)
    except (ValueError, AttributeError):
        raise ValueError("periodo inválido (usa AAAA-MM)") from None
    return first, date(year, month, calendar.monthrange(year, month)[1])


def previous_periodo(today: date) -> str:
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{year:04d}-{month:02d}"


def get_snapshot(db: Session, periodo: str) -> Optional[AvanceCarteraSnapshot]:
    return db.query(AvanceCarteraSnapshot).filter(AvanceCarteraSnapshot.periodo == periodo).first()


def take_snapshot(
    db: Session, client, periodo: str, *, today: Optional[date] = None, taken_by: Optional[int] = None
) -> AvanceCarteraSnapshot:
    """(Re)take the snapshot of a whole, already ended month. Raises ``ValueError``,
    ``PeriodoSinFoto``, ``PeriodoNoTerminado``, ``PeriodoCerrado`` or ``SysgalError`` (nothing is written then)."""
    desde, hasta = periodo_bounds(periodo)
    if periodo < FOTO_DESDE:
        raise PeriodoSinFoto(periodo)
    if (today or date.today()) <= hasta:
        raise PeriodoNoTerminado(periodo)
    if cierre_svc.is_cerrado(db, periodo):
        raise PeriodoCerrado(periodo)

    payload = client.estadisticas_cartera(desde, hasta)

    taken_at = datetime.utcnow()
    snap = get_snapshot(db, periodo)
    if snap is None:
        db.add(
            AvanceCarteraSnapshot(
                periodo=periodo, desde=desde, hasta=hasta, payload=payload, taken_at=taken_at, taken_by=taken_by
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # Raced the scheduler (or another POST): the row exists now, so update it instead.
            db.rollback()
            snap = get_snapshot(db, periodo)
            if snap is None:
                raise
    if snap is not None:
        snap.desde, snap.hasta = desde, hasta
        snap.payload, snap.taken_at, snap.taken_by = payload, taken_at, taken_by
        db.commit()
    snap = get_snapshot(db, periodo)
    db.refresh(snap)
    return snap


def take_previous_month_if_missing(db: Session, client, today: date) -> str:
    """Scheduler entry: snapshot of the previous month once. Returns ``taken|exists|closed|before_start``."""
    periodo = previous_periodo(today)
    if periodo < FOTO_DESDE:
        return "before_start"
    if get_snapshot(db, periodo) is not None:
        return "exists"
    if cierre_svc.is_cerrado(db, periodo):
        return "closed"
    take_snapshot(db, client, periodo, today=today)
    return "taken"


def cobertura_by_lawyer(db: Session, periodo: str) -> Optional[dict[int, float]]:
    """``{lawyer_id: cobertura}`` from the período's snapshot, or ``None`` when there is none.

    Only Sysgal abogados mapped to a lawyer appear; lawyers absent from it are the caller's 0.
    """
    if periodo < FOTO_DESDE:
        return None  # defense in depth: those months always use the manual weeks
    snap = get_snapshot(db, periodo)
    if snap is None:
        return None
    lawyer_by_sysgal = {
        sid: lid
        for sid, lid in db.query(SysgalUsuario.sysgal_id, SysgalUsuario.lawyer_id)
        .filter(SysgalUsuario.lawyer_id.isnot(None))
        .all()
    }
    rows_by_lawyer: dict[int, list[dict]] = {}
    seen_abogados: set = set()
    for row in (snap.payload or {}).get("por_abogado", []):
        abogado_id = row.get("abogado_id")
        if abogado_id in seen_abogados:
            continue  # a repeated abogado_id row counts once
        seen_abogados.add(abogado_id)
        lid = lawyer_by_sysgal.get(abogado_id)
        if lid is not None:
            rows_by_lawyer.setdefault(lid, []).append(row)

    out: dict[int, float] = {}
    for lid, rows in rows_by_lawyer.items():
        if len(rows) == 1:
            out[lid] = float(rows[0].get("cobertura") or 0)
            continue
        # Several Sysgal users mapped to one lawyer: combine from the counts, deterministically.
        total = sum(int(r.get("total") or 0) for r in rows)
        gestionadas = sum(int(r.get("gestionadas") or 0) for r in rows)
        out[lid] = round(100 * gestionadas / total, 2) if total > 0 else 0.0
    return out
