"""Incorporation queue: Sysgal causas the firm holds that our system does not follow yet.

``encolar_incorporaciones`` only reads ``sysgal_causas`` / ``courts`` and writes
``sysgal_incorporaciones``; it never talks to Sysgal or PJUD. Idempotent.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.hitos import _tribunal_key
from app.models.court import Court
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.services.sysgal_matching import AMBIGUA

logger = logging.getLogger(__name__)

PENDIENTE = "pendiente"
INCORPORADA = "incorporada"
SIN_CORTE = "sin_corte"

_VALID_ROL = re.compile(r"^[A-Z]-\d+-\d{4}$")


def rol_year(rol_norm: str) -> int:
    return int(rol_norm.rsplit("-", 1)[1])


def _court_resolver(db: Session):
    """Return ``resolve(tribunal, codigo) -> Court | None`` using the matcher's tribunal key."""
    courts = db.query(Court).all()
    by_code = {c.code: c for c in courts}
    by_key: dict[str, list[Court]] = {}
    for c in courts:
        key = _tribunal_key(c.name)
        if key:
            by_key.setdefault(key, []).append(c)

    def resolve(tribunal: Optional[str], codigo: Optional[str]) -> Optional[Court]:
        if codigo and codigo in by_code:
            return by_code[codigo]
        hits = by_key.get(_tribunal_key(tribunal) or "", [])
        return hits[0] if len(hits) == 1 else None

    return resolve


def _apply_court(row: SysgalIncorporacion, court: Optional[Court]) -> None:
    row.court_id = court.id if court else None
    row.corte = court.pjud_corte if court else None
    row.estado = PENDIENTE if court is not None and court.pjud_corte is not None else SIN_CORTE


def _rank(row: SysgalCausa):
    updated = row.sysgal_updated_at
    return (-rol_year(row.rol_norm), -(updated.timestamp() if updated else 0.0), row.id)


def encolar_incorporaciones(db: Session) -> dict:
    now = datetime.utcnow()
    stats = {"encoladas": 0, "sin_corte": 0, "incorporadas": 0, "reevaluadas": 0}
    resolve = _court_resolver(db)

    # 1. Queued causas that got matched meanwhile are done.
    matched = (
        db.query(SysgalIncorporacion, SysgalCausa.case_id)
        .join(SysgalCausa, SysgalCausa.id == SysgalIncorporacion.sysgal_causa_id)
        .filter(SysgalCausa.case_id.isnot(None), SysgalIncorporacion.estado != INCORPORADA)
        .all()
    )
    for q, case_id in matched:
        q.estado, q.case_id = INCORPORADA, case_id
        stats["incorporadas"] += 1

    # 2. sin_corte rows whose court can now be resolved go back to pending.
    for q, causa in (
        db.query(SysgalIncorporacion, SysgalCausa)
        .join(SysgalCausa, SysgalCausa.id == SysgalIncorporacion.sysgal_causa_id)
        .filter(SysgalIncorporacion.estado == SIN_CORTE)
        .all()
    ):
        _apply_court(q, resolve(causa.tribunal, causa.tribunal_codigo))
        if q.estado == PENDIENTE:
            stats["reevaluadas"] += 1

    # 3. New eligible causas.
    queued = db.query(SysgalIncorporacion.sysgal_causa_id)
    candidates = (
        db.query(SysgalCausa)
        .filter(
            func.lower(SysgalCausa.estado) == "activa",
            SysgalCausa.eliminada.is_(False),
            SysgalCausa.case_id.is_(None),
            (SysgalCausa.match_method.is_(None)) | (SysgalCausa.match_method != AMBIGUA),
            SysgalCausa.id.notin_(queued),
        )
        .all()
    )
    for causa in candidates:
        if not _VALID_ROL.match(causa.rol_norm or ""):
            continue
        row = SysgalIncorporacion(sysgal_causa_id=causa.id, created_at=now, updated_at=now)
        _apply_court(row, resolve(causa.tribunal, causa.tribunal_codigo))
        db.add(row)
        stats["encoladas"] += 1
        if row.estado == SIN_CORTE:
            stats["sin_corte"] += 1
    db.flush()

    # 4. Re-rank every pending row: year desc, then most recently updated in Sysgal.
    pending = (
        db.query(SysgalIncorporacion, SysgalCausa)
        .join(SysgalCausa, SysgalCausa.id == SysgalIncorporacion.sysgal_causa_id)
        .filter(SysgalIncorporacion.estado == PENDIENTE)
        .all()
    )
    pending.sort(key=lambda pair: _rank(pair[1]))
    for position, (q, _causa) in enumerate(pending, start=1):
        if q.prioridad != position:
            q.prioridad = position
    db.flush()

    logger.info("Sysgal incorporacion queue: %s", stats)
    return stats
