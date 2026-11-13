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
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.api.v1.hitos import _normalize_rol_text, _tribunal_key
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.utils.rut import clean_rut

CALZADA = "calzada"
SIN_CALCE = "sin_calce"
AMBIGUA = "ambigua"

_CHUNK = 5000


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
    rows = _case_rows(db, [rol_norm]).get(rol_norm, [])
    return _decide(rows, tribunal, tribunal_codigo, rut_cliente, lambda ids: _ruts_by_case(db, ids))


def match_causas(db: Session, items: Sequence[tuple]) -> list[MatchResult]:
    """Bulk twin of ``match_causa``: same decisions for many ``(rol_norm, tribunal, tribunal_codigo,
    rut_cliente)`` items, with a constant number of queries (cases by ROL, then litigantes of the
    candidate cases that need the RUT step), chunked for very large inputs."""
    if not items:
        return []
    by_rol = _case_rows(db, sorted({item[0] for item in items if item[0]}))
    ruts: dict[int, list[str]] = {}
    fetched: set[int] = set()

    def ruts_for(ids: list[int]) -> dict[int, list[str]]:
        missing = [i for i in ids if i not in fetched]
        if missing:
            ruts.update(_ruts_by_case(db, missing))
            fetched.update(missing)
        return {i: ruts.get(i, []) for i in ids}

    results = []
    for rol_norm, tribunal, codigo, rut in items:
        if not rol_norm:
            results.append(MatchResult(SIN_CALCE))
            continue
        results.append(_decide(by_rol.get(rol_norm, []), tribunal, codigo, rut, ruts_for))
    return results


def _case_rows(db: Session, rols: list[str]) -> dict[str, list]:
    """``{rol: [(case_id, court_code, court_name, court_id, procedure), ...]}`` ordered by case id."""
    out: dict[str, list] = {}
    for start in range(0, len(rols), _CHUNK):
        rows = (
            db.query(Case.id, Court.code, Court.name, Case.court_id, Case.procedure, Case.rol)
            .join(Court, Court.id == Case.court_id)
            .filter(Case.rol.in_(rols[start : start + _CHUNK]))
            .order_by(Case.id)
            .all()
        )
        for row in rows:
            out.setdefault(row[5], []).append(row[:5])
    return out


def _decide(rows, tribunal, tribunal_codigo, rut_cliente, ruts_for) -> MatchResult:
    if not rows:
        return MatchResult(SIN_CALCE)

    by_tribunal = _tribunal_hits(rows, tribunal, tribunal_codigo)
    if len(by_tribunal) == 1:
        return MatchResult(CALZADA, by_tribunal[0], "rol+tribunal")

    pool = by_tribunal or [r[0] for r in rows]
    hits = _rut_hits(ruts_for, pool, rut_cliente)
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


def _ruts_by_case(db: Session, case_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for start in range(0, len(case_ids), _CHUNK):
        rows = (
            db.query(CaseLitigante.case_id, CaseLitigante.rut)
            .filter(CaseLitigante.case_id.in_(case_ids[start : start + _CHUNK]))
            .all()
        )
        for cid, rut in rows:
            out.setdefault(cid, []).append(rut)
    return out


def _rut_hits(ruts_for, case_ids: list[int], rut_cliente: Optional[str]) -> list[int]:
    if not rut_cliente:
        return []
    wanted = clean_rut(rut_cliente)
    by_case = ruts_for(case_ids)
    return sorted({cid for cid in case_ids for rut in by_case.get(cid, []) if rut and clean_rut(rut) == wanted})
