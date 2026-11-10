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
   Exception: when those hits are cuadernos of one causa (same court; our system stores one row
   per cuaderno) and EXACTLY ONE has ``procedure == 'Principal'``, that one wins (same method as
   the deciding step). Zero or several Principal -> still ``ambigua``.
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
        db.query(Case.id, Court.code, Court.name, Case.court_id, Case.procedure)
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
    if len(hits) > 1:
        principal = _sole_principal(rows, hits)
        return MatchResult(CALZADA, principal, "rol+rut") if principal else MatchResult(AMBIGUA)
    if len(by_tribunal) > 1:
        principal = _sole_principal(rows, by_tribunal)
        return MatchResult(CALZADA, principal, "rol+tribunal") if principal else MatchResult(AMBIGUA)
    return MatchResult(SIN_CALCE)


def _sole_principal(rows, case_ids: list[int]) -> Optional[int]:
    """The only 'Principal' cuaderno among ``case_ids`` when they all sit in one court, else None."""
    wanted = set(case_ids)
    candidates = [r for r in rows if r[0] in wanted]
    if len({r[3] for r in candidates}) != 1:
        return None
    principals = [r[0] for r in candidates if (r[4] or "").strip().lower() == "principal"]
    return principals[0] if len(principals) == 1 else None


def _tribunal_hits(rows, tribunal: Optional[str], tribunal_codigo: Optional[str]) -> list[int]:
    if tribunal_codigo:
        hits = [r[0] for r in rows if r[1] == tribunal_codigo]
        if hits:
            return hits
    key = _tribunal_key(tribunal)
    if not key:
        return []
    return [r[0] for r in rows if _tribunal_key(r[2]) == key]


def _rut_hits(db: Session, case_ids: list[int], rut_cliente: Optional[str]) -> list[int]:
    if not rut_cliente:
        return []
    wanted = clean_rut(rut_cliente)
    ruts = db.query(CaseLitigante.case_id, CaseLitigante.rut).filter(CaseLitigante.case_id.in_(case_ids)).all()
    return sorted({cid for cid, rut in ruts if rut and clean_rut(rut) == wanted})
