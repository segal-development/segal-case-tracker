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
DESCARTADA = "descartada"

_VALID_ROL = re.compile(r"^[A-Z]-\d+-\d{4}$")


def rol_year(rol_norm: str) -> int:
    return int(rol_norm.rsplit("-", 1)[1])


_ORDINAL_WORDS = {
    "primer": 1, "primero": 1, "segundo": 2, "tercer": 3, "tercero": 3, "cuarto": 4,
    "quinto": 5, "sexto": 6, "séptimo": 7, "septimo": 7, "octavo": 8, "noveno": 9, "décimo": 10, "decimo": 10,
}
_ORDINAL_TOKEN = re.compile(r"^(\d{1,2})(er|ero|ro|do|to|vo|no|mo|ndo|nvo)$", re.IGNORECASE)
# Pure criminal courts only: "Juzgado de Letras y Garantía" is mixed and takes civil causas.
_GARANTIA = re.compile(r"juzgado\s+de\s+garant[ií]a", re.IGNORECASE)
TRIBUNAL_PENAL = "tribunal penal"


def _fold_ordinals(text: str) -> str:
    """Rewrite written Spanish ordinals ("1er", "2do", "Primer"…) as "Nº" tokens (whole token only)."""
    out = []
    for token in text.split():
        m = _ORDINAL_TOKEN.match(token)
        if m and 1 <= int(m.group(1)) <= 30:
            out.append(f"{int(m.group(1))}º")
        elif token.lower() in _ORDINAL_WORDS:
            out.append(f"{_ORDINAL_WORDS[token.lower()]}º")
        else:
            out.append(token)
    return " ".join(out)


def _queue_key(tribunal: Optional[str]) -> Optional[str]:
    key = _tribunal_key(tribunal)
    return _tribunal_key(_fold_ordinals(key)) if key else None


def _city_of(name: str) -> str:
    parts = re.split(r"\s+de\s+", name.strip(), flags=re.IGNORECASE)
    return " ".join(parts[-1].split()).upper() if len(parts) > 1 else ""


def _is_penal(*names: Optional[str]) -> bool:
    return any(n and _GARANTIA.search(n) for n in names)


def _court_resolver(db: Session):
    """Return ``resolve(tribunal, codigo) -> (Court | None, corte | None, penal)``.

    The corte is ``Court.pjud_corte`` or, when the court lacks it, the one value every other
    court of the same city agrees on (never written back to ``courts``).
    """
    courts = sorted(db.query(Court).all(), key=lambda c: c.id)
    by_code = {c.code: c for c in courts}
    by_key: dict[str, list[Court]] = {}
    cortes_by_city: dict[str, set[int]] = {}
    for c in courts:
        key = _queue_key(c.name)
        if key:
            by_key.setdefault(key, []).append(c)
        city = _city_of(c.name or "")
        if city and c.pjud_corte is not None:
            cortes_by_city.setdefault(city, set()).add(c.pjud_corte)

    def pick(hits: list[Court]) -> Optional[Court]:
        if len(hits) == 1:
            return hits[0]
        # Same tribunal modulo case/spaces/ordinal sign (the key) -> prefer the one with a corte.
        return next((c for c in hits if c.pjud_corte is not None), hits[0]) if hits else None

    def resolve(tribunal: Optional[str], codigo: Optional[str]):
        court = by_code.get(codigo) if codigo else None
        if court is None:
            court = pick(by_key.get(_queue_key(tribunal) or "", []))
        if _is_penal(tribunal, court.name if court else None):
            return court, None, True
        if court is None:
            return None, None, False
        corte = court.pjud_corte
        if corte is None:
            siblings = cortes_by_city.get(_city_of(court.name or ""), set())
            corte = next(iter(siblings)) if len(siblings) == 1 else None
        return court, corte, False

    return resolve


def _apply_court(row: SysgalIncorporacion, resolved) -> None:
    court, corte, penal = resolved
    row.court_id = court.id if court else None
    row.corte = corte
    if penal:
        row.estado, row.ultimo_error = DESCARTADA, TRIBUNAL_PENAL
        return
    row.estado = PENDIENTE if court is not None and corte is not None else SIN_CORTE


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
