"""Contract state per case (``cases.contrato_estado``), computed in ONE place.

Precedence:
1. ``rut_cliente`` of the case's OWN active, non-eliminada Sysgal causa (latest by update, ties: highest
   id) looked up in ``cliente_sysgal_estados``. Used only when that RUT has a cache row (an unanswered RUT
   says nothing about the contract, so it falls through).
2. The DDO natural-person RUT inference (``ddo_info_by_case``): the best cobertura among the defendants.
3. ``sin_dato``.

Every state goes through ``derive_cobertura`` (stale ACTIVO -> caducado). ``recompute_contrato_estado``
persists the result (and thereby ``es_activa``, through the Case mapper listener contract: it writes both
columns explicitly) without touching ``updated_at`` or any other field.
"""

import logging
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import bindparam, func, update
from sqlalchemy.orm import Session

from app.models.case import Case, compute_es_activa
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.sysgal_causa import SysgalCausa
from app.services.sysgal_cobertura import COBERTURAS, derive_cobertura
from app.utils.rut import clean_rut

logger = logging.getLogger(__name__)

_COBERTURA_PRIORITY = {c: i for i, c in enumerate(COBERTURAS)}
_ACTIVA = "activa"
_CHUNK = 5000


def _info(row: Optional[ClienteSysgalEstado], today: date) -> dict:
    if row is None:
        return {"cobertura": "sin_dato", "estado_codigo": None, "vigencia_hasta": None, "synced_at": None}
    return {
        "cobertura": derive_cobertura(row.estado_codigo, row.vigencia_hasta, row.encontrado, today),
        "estado_codigo": row.estado_codigo,
        "vigencia_hasta": row.vigencia_hasta,
        "synced_at": row.synced_at,
    }


def ddo_info_by_case(db: Session, case_ids=None, today: Optional[date] = None) -> dict:
    """Cobertura per case from its defendants' RUTs (the inference used while Sysgal sends no client).

    ``case_ids``: list or subquery; ``None`` = every case. Two queries total, never N+1.

    Returns ``{case_id: {"cobertura", "estado_codigo", "vigencia_hasta", "synced_at"}}`` only for cases with
    at least one usable DDO RUT; such a RUT with no cache row derives to ``sin_dato``.

    **Company parties are skipped: the demandado recorded there is often the creditor, not the client**
    (93.9% of JURIDICA demandados are unknown to Sysgal versus 9.1% of natural persons; one credit issuer
    is demandado on 338 causas). Since the best cobertura among the parties wins, a creditor resolving as
    ACTIVO would mask a client whose contract lapsed. Excluding what is known to be a company (rather than
    allowing a closed list) means an unforeseen ``persona_type`` still gets looked at.
    """
    query = db.query(CaseLitigante.case_id, CaseLitigante.rut).filter(
        CaseLitigante.participante.ilike("DDO%"),
        CaseLitigante.persona_type != "JURIDICA",
        CaseLitigante.rut != "",
    )
    if case_ids is not None:
        query = query.filter(CaseLitigante.case_id.in_(case_ids))
    ruts_by_case: dict[int, set[str]] = {}
    for case_id, raw_rut in query.all():
        rut = clean_rut(raw_rut) if raw_rut else ""
        if rut:
            ruts_by_case.setdefault(case_id, set()).add(rut)
    if not ruts_by_case:
        return {}

    cache = _cache_rows(db, set().union(*ruts_by_case.values()))
    today = today or date.today()
    result: dict[int, dict] = {}
    for case_id, ruts in ruts_by_case.items():
        best = None
        for rut in sorted(ruts):
            info = _info(cache.get(rut), today)
            if best is None or _COBERTURA_PRIORITY[info["cobertura"]] < _COBERTURA_PRIORITY[best["cobertura"]]:
                best = info
        result[case_id] = best
    return result


def _cache_rows(db: Session, ruts: set[str]) -> dict[str, ClienteSysgalEstado]:
    ordered = sorted(ruts)
    out: dict[str, ClienteSysgalEstado] = {}
    for start in range(0, len(ordered), _CHUNK):
        for row in db.query(ClienteSysgalEstado).filter(ClienteSysgalEstado.rut.in_(ordered[start : start + _CHUNK])):
            out[row.rut] = row
    return out


def _sysgal_rut_by_case(db: Session, case_ids: Optional[list[int]]) -> dict[int, str]:
    """Canonical ``rut_cliente`` of each case's own latest active, non-eliminada Sysgal causa."""
    query = (
        db.query(SysgalCausa.case_id, SysgalCausa.rut_cliente)
        .filter(
            SysgalCausa.case_id.isnot(None),
            SysgalCausa.eliminada.is_(False),
            func.lower(func.trim(SysgalCausa.estado)) == _ACTIVA,
            SysgalCausa.rut_cliente.isnot(None),
        )
        .order_by(func.coalesce(SysgalCausa.sysgal_updated_at, datetime.min), SysgalCausa.id)
    )
    if case_ids is not None:
        query = query.filter(SysgalCausa.case_id.in_(case_ids))
    latest: dict[int, str] = {}
    for case_id, raw in query.all():  # later rows overwrite earlier ones
        rut = clean_rut(raw) if raw else ""
        if rut:
            latest[case_id] = rut
        else:
            latest.pop(case_id, None)
    return latest


def compute_contrato_estados(
    db: Session, case_ids: Optional[Iterable[int]] = None, today: Optional[date] = None
) -> dict[int, str]:
    """``{case_id: estado}`` for the given cases (every case when ``None``)."""
    ids = None if case_ids is None else sorted(set(case_ids))
    if ids is None:
        all_ids = [cid for (cid,) in db.query(Case.id).all()]
        scope = None
    else:
        all_ids = ids
        scope = ids
    if not all_ids:
        return {}
    today = today or date.today()

    sysgal_ruts = _sysgal_rut_by_case(db, scope)
    cache = _cache_rows(db, set(sysgal_ruts.values()))
    ddo = ddo_info_by_case(db, scope, today)

    result: dict[int, str] = {}
    for cid in all_ids:
        row = cache.get(sysgal_ruts.get(cid, ""))
        if row is not None:
            result[cid] = _info(row, today)["cobertura"]
        elif cid in ddo:
            result[cid] = ddo[cid]["cobertura"]
        else:
            result[cid] = "sin_dato"
    return result


def recompute_contrato_estado(
    db: Session,
    case_ids: Optional[Iterable[int]] = None,
    today: Optional[date] = None,
    dry_run: bool = False,
) -> dict:
    """Recompute ``contrato_estado`` (and ``es_activa``) and write ONLY the cases that changed.

    ``dry_run`` writes and commits nothing. Returns ``{"total", "cambiadas", "por_estado",
    "antes_por_estado", "activas_antes", "activas_despues"}``. ``updated_at`` and every other column are
    left as they were (the list is ordered by ``updated_at``).
    """
    estados = compute_contrato_estados(db, case_ids, today)
    current: dict[int, tuple] = {}
    ids = sorted(estados)
    for start in range(0, len(ids), _CHUNK):
        for row in db.query(
            Case.id, Case.contrato_estado, Case.terminada_en_sysgal, Case.es_activa
        ).filter(Case.id.in_(ids[start : start + _CHUNK])):
            current[row.id] = (row.contrato_estado, bool(row.terminada_en_sysgal), bool(row.es_activa))

    por_estado = {e: 0 for e in COBERTURAS}
    antes = {e: 0 for e in COBERTURAS}
    changes = []
    activas_antes = activas_despues = 0
    for cid, estado in estados.items():
        if cid not in current:
            continue
        old_estado, terminada, old_activa = current[cid]
        por_estado[estado] += 1
        antes[old_estado or "sin_dato"] += 1
        nueva = compute_es_activa(terminada, estado)
        activas_antes += old_activa
        activas_despues += nueva
        if old_estado != estado or old_activa != nueva:
            changes.append({"b_id": cid, "b_estado": estado, "b_activa": nueva})

    if changes and not dry_run:
        table = Case.__table__
        # Core UPDATE (not the ORM bulk path): ``updated_at`` is set to itself so its onupdate never fires.
        stmt = (
            update(table)
            .where(table.c.id == bindparam("b_id"))
            .values(contrato_estado=bindparam("b_estado"), es_activa=bindparam("b_activa"), updated_at=table.c.updated_at)
        )
        conn = db.connection()
        for start in range(0, len(changes), _CHUNK):
            conn.execute(stmt, changes[start : start + _CHUNK])
        db.commit()
    elif dry_run:
        db.rollback()

    return {
        "total": len(current),
        "cambiadas": len(changes),
        "por_estado": por_estado,
        "antes_por_estado": antes,
        "activas_antes": activas_antes,
        "activas_despues": activas_despues,
    }


def recompute_safely(db: Session, case_ids: Optional[Iterable[int]] = None) -> int:
    """Worker hook: recompute, never raise (the data feeding it is already committed). Returns changes."""
    try:
        return recompute_contrato_estado(db, case_ids)["cambiadas"]
    except Exception:
        db.rollback()
        logger.exception("contrato_estado recompute failed (non-fatal)")
        return 0
