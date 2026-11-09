"""Match a Sysgal causa to one of OUR ``Case`` rows.

Rules (never guess — ambiguity leaves ``case_id`` NULL):

1. ROL (normalized like the hitos ROL: ``100-2026`` -> ``C-100-2026``) narrows the candidates.
2. Among them, tribunal: ``tribunal_codigo == courts.code`` when Sysgal sends it, else the
   hitos tribunal key (fold °/º/ª, collapse spaces, uppercase) of Sysgal's tribunal vs the
   court name. Exactly one hit -> ``rol+tribunal``.
3. Otherwise (zero or several tribunal hits) the client RUT among ``case_litigantes``
   decides, over the tribunal hits when there are some, else over every case with that ROL.
   Exactly one hit -> ``rol+rut``.
4. More than one hit at the deciding step -> ``ambigua``; no candidate -> ``sin_calce``.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.api.v1.hitos import _normalize_rol_text, _tribunal_key
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.utils.rut import clean_rut

CALZADA = "calzada"
SIN_CALCE = "sin_calce"
AMBIGUA = "ambigua"


@dataclass
class MatchResult:
    status: str
    case_id: Optional[int] = None
    method: Optional[str] = None


def normalize_rol(rol: Optional[str]) -> str:
    """Same normalization the hitos use for a ROL; empty string for empty input."""
    return _normalize_rol_text(rol) or ""


def match_causa(
    db: Session,
    rol_norm: str,
    tribunal: Optional[str],
    tribunal_codigo: Optional[str],
    rut_cliente: Optional[str],
) -> MatchResult:
    if not rol_norm:
        return MatchResult(SIN_CALCE)

    rows = (
        db.query(Case.id, Court.code, Court.name)
        .join(Court, Court.id == Case.court_id)
        .filter(Case.rol == rol_norm)
        .order_by(Case.id)
        .all()
    )
    if not rows:
        return MatchResult(SIN_CALCE)

    by_tribunal = _tribunal_hits(rows, tribunal, tribunal_codigo)
    if len(by_tribunal) == 1:
        return MatchResult(CALZADA, by_tribunal[0], "rol+tribunal")

    pool = by_tribunal or [r[0] for r in rows]
    hits = _rut_hits(db, pool, rut_cliente)
    if len(hits) == 1:
        return MatchResult(CALZADA, hits[0], "rol+rut")
    if len(hits) > 1 or len(by_tribunal) > 1:
        return MatchResult(AMBIGUA)
    return MatchResult(SIN_CALCE)


def _tribunal_hits(rows, tribunal: Optional[str], tribunal_codigo: Optional[str]) -> list[int]:
    if tribunal_codigo:
        hits = [cid for cid, code, _ in rows if code == tribunal_codigo]
        if hits:
            return hits
    key = _tribunal_key(tribunal)
    if not key:
        return []
    return [cid for cid, _, name in rows if _tribunal_key(name) == key]


def _rut_hits(db: Session, case_ids: list[int], rut_cliente: Optional[str]) -> list[int]:
    if not rut_cliente:
        return []
    wanted = clean_rut(rut_cliente)
    ruts = db.query(CaseLitigante.case_id, CaseLitigante.rut).filter(CaseLitigante.case_id.in_(case_ids)).all()
    return sorted({cid for cid, rut in ruts if rut and clean_rut(rut) == wanted})
