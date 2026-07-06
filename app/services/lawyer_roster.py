"""Firm roster helpers: co-side firm lawyers for the authenticated account."""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.lawyer import Lawyer
from app.utils.rut import normalize_rut

DEMANDANTE_ABOGADO = frozenset({"AB.DTE", "AP.DTE"})
DEMANDADO_ABOGADO = frozenset({"AB.DDO", "AP.DDO"})
ALL_ABOGADO = DEMANDANTE_ABOGADO | DEMANDADO_ABOGADO

_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_nombre(nombre: str) -> str:
    return _TRAILING_PAREN_RE.sub("", nombre or "").strip()


def _abogado_litigantes_by_case(db: Session, *, competencia: str = "civil") -> dict[int, list]:
    """Load all abogado-coded litigantes across ALL firm cases, grouped by case_id.

    Firm-wide (Approach C): does NOT filter by ``Case.lawyer_id`` — under the
    unified ownership model every Case's ``lawyer_id`` is the firm's bookkeeping
    owner, not the abogado who sees/owns it. Attribution is entirely
    litigante-derived, so the candidate case set spans every firm case
    (bounded by ``competencia``), independent of who synced it.
    """
    rows = (
        db.query(CaseLitigante)
        .join(Case, Case.id == CaseLitigante.case_id)
        .filter(
            Case.competencia == competencia,
            CaseLitigante.participante.in_(list(ALL_ABOGADO)),
        )
        .all()
    )
    by_case: dict[int, list] = defaultdict(list)
    for row in rows:
        by_case[row.case_id].append(row)
    return dict(by_case)


def firm_roster(db: Session, account_rut: str) -> list[dict]:
    """Return firm lawyers (co-side abogados) across the account's cases.

    For each distinct abogado RUT appearing on the SAME side as the account
    in at least one case, returns {rut, nombre (trailing paren stripped), case_count}.
    Sorted by case_count desc.
    """
    account_rut_norm = normalize_rut(account_rut)
    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return []

    by_case = _abogado_litigantes_by_case(db)

    abogado_cases: dict[str, set[int]] = defaultdict(set)
    abogado_info: dict[str, dict] = {}

    for case_id, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is None:
            continue
        # Include the account holder too — they are a firm lawyer with their own
        # caseload. Every firm-side abogado (account included) is a roster member.
        for lit in litigantes:
            norm = normalize_rut(lit.rut)
            if lit.participante in account_side:
                abogado_cases[norm].add(case_id)
                if norm not in abogado_info:
                    abogado_info[norm] = {"rut": norm, "nombre": _clean_nombre(lit.nombre)}

    result = [
        {"rut": abogado_info[n]["rut"], "nombre": abogado_info[n]["nombre"], "case_count": len(ids)}
        for n, ids in abogado_cases.items()
    ]
    result.sort(key=lambda x: x["case_count"], reverse=True)
    return result


def firm_dashboard_stats(db: Session, account_rut: str) -> dict:
    """Return firm-wide dashboard stats for the authenticated account.

    Aggregates semaforo distribution, staleness, materia breakdown, and
    per-lawyer metrics across all civil cases belonging to the account's firm.
    """
    _EMPTY = {
        "totals": {
            "cases": 0,
            "semaforo": {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0},
            "stale": 0,
            "by_materia": [],
            "by_procedural_state": [],
        },
        "by_lawyer": [],
    }

    account_rut_norm = normalize_rut(account_rut)
    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return _EMPTY

    by_case = _abogado_litigantes_by_case(db)

    # Build abogado_cases and abogado_info (same side-resolution as firm_roster)
    abogado_cases: dict[str, set[int]] = defaultdict(set)
    abogado_info: dict[str, dict] = {}

    for case_id, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is None:
            continue
        for lit in litigantes:
            norm = normalize_rut(lit.rut)
            if lit.participante in account_side:
                abogado_cases[norm].add(case_id)
                if norm not in abogado_info:
                    abogado_info[norm] = {"rut": norm, "nombre": _clean_nombre(lit.nombre)}

    # Load civil cases in one query
    cases = (
        db.query(Case.id, Case.semaforo, Case.last_movement_at, Case.procedure, Case.procedural_state)
        .filter(Case.lawyer_id == lawyer.id, Case.competencia == "civil")
        .all()
    )
    case_map = {c.id: c for c in cases}

    stale_cutoff = datetime.utcnow() - timedelta(days=30)

    def _sem_bucket(sem: Optional[str]) -> str:
        return sem if sem in ("rojo", "amarillo", "verde") else "otros"

    def _is_stale(lma: Optional[datetime]) -> bool:
        return lma is None or lma < stale_cutoff

    # Aggregate totals across all civil cases
    sem_totals: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
    stale_total = 0
    materia_counts: defaultdict[str, int] = defaultdict(int)

    for c in cases:
        sem_totals[_sem_bucket(c.semaforo)] += 1
        if _is_stale(c.last_movement_at):
            stale_total += 1
        # PJUD populates "Procedimiento" (procedure), not "Materia" (matter is
        # always null), so the breakdown groups by procedure. Label stays
        # "materia" for the frontend, which already treats procedure as such.
        materia_counts[c.procedure or "Sin materia"] += 1

    by_materia = sorted(
        [{"materia": m, "count": cnt} for m, cnt in materia_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:12]

    PROCEDURAL_ORDER = [
        "mandamiento", "notificado", "excepciones", "traslado_ejecutante",
        "admisibilidad", "auto_prueba", "citacion_sentencia", "sentencia",
        "rebelde", "terminada", "indeterminate", "sin_clasificar",
    ]
    stage_counts: dict[str, int] = defaultdict(int)
    for c in cases:
        stage = c.procedural_state if c.procedural_state is not None else "sin_clasificar"
        stage_counts[stage] += 1

    known = set(PROCEDURAL_ORDER)
    unknown_stages = sorted(s for s in stage_counts if s not in known)
    ordered_stages = (
        [s for s in PROCEDURAL_ORDER[:-1] if s in stage_counts]
        + unknown_stages
        + (["sin_clasificar"] if "sin_clasificar" in stage_counts else [])
    )
    by_procedural_state = [{"stage": s, "count": stage_counts[s]} for s in ordered_stages]

    # Aggregate per-lawyer stats restricted to civil cases
    by_lawyer = []
    for norm_rut, case_ids in abogado_cases.items():
        civil_ids = [cid for cid in case_ids if cid in case_map]
        if not civil_ids:
            continue
        lsem: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
        lstale = 0
        for cid in civil_ids:
            c = case_map[cid]
            lsem[_sem_bucket(c.semaforo)] += 1
            if _is_stale(c.last_movement_at):
                lstale += 1
        info = abogado_info.get(norm_rut, {"rut": norm_rut, "nombre": ""})
        by_lawyer.append({
            "rut": info["rut"],
            "nombre": info["nombre"],
            "case_count": len(civil_ids),
            "rojo": lsem["rojo"],
            "amarillo": lsem["amarillo"],
            "verde": lsem["verde"],
            "otros": lsem["otros"],
            "stale": lstale,
        })

    by_lawyer.sort(key=lambda x: x["case_count"], reverse=True)

    return {
        "totals": {
            "cases": len(cases),
            "semaforo": sem_totals,
            "stale": stale_total,
            "by_materia": by_materia,
            "by_procedural_state": by_procedural_state,
        },
        "by_lawyer": by_lawyer,
    }


def firm_dashboard_stats_all(db: Session) -> dict:
    """Firm-wide dashboard stats spanning EVERY study case, across ALL lawyers.

    Used for the auditor role, a transversal role overseeing the whole
    firm's caseload. Unlike ``firm_dashboard_stats`` (single account +
    co-side litigante inference — the model that fit when one account
    scraped the entire firm's caseload), this aggregates directly over
    every civil ``Case`` row grouped by its own ``Case.lawyer_id``: cases
    are now attributed per-lawyer, so ownership is already explicit and no
    litigante-side inference is needed.
    """
    cases = (
        db.query(
            Case.id,
            Case.lawyer_id,
            Case.semaforo,
            Case.last_movement_at,
            Case.procedure,
            Case.procedural_state,
        )
        .filter(Case.competencia == "civil")
        .all()
    )

    def _sem_bucket(sem: Optional[str]) -> str:
        return sem if sem in ("rojo", "amarillo", "verde") else "otros"

    stale_cutoff = datetime.utcnow() - timedelta(days=30)

    def _is_stale(lma: Optional[datetime]) -> bool:
        return lma is None or lma < stale_cutoff

    sem_totals: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
    stale_total = 0
    materia_counts: defaultdict[str, int] = defaultdict(int)
    stage_counts: defaultdict[str, int] = defaultdict(int)
    by_lawyer_cases: defaultdict[int, list] = defaultdict(list)

    for c in cases:
        sem_totals[_sem_bucket(c.semaforo)] += 1
        if _is_stale(c.last_movement_at):
            stale_total += 1
        materia_counts[c.procedure or "Sin materia"] += 1
        stage = c.procedural_state if c.procedural_state is not None else "sin_clasificar"
        stage_counts[stage] += 1
        by_lawyer_cases[c.lawyer_id].append(c)

    by_materia = sorted(
        [{"materia": m, "count": cnt} for m, cnt in materia_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:12]

    PROCEDURAL_ORDER = [
        "mandamiento", "notificado", "excepciones", "traslado_ejecutante",
        "admisibilidad", "auto_prueba", "citacion_sentencia", "sentencia",
        "rebelde", "terminada", "indeterminate", "sin_clasificar",
    ]
    known = set(PROCEDURAL_ORDER)
    unknown_stages = sorted(s for s in stage_counts if s not in known)
    ordered_stages = (
        [s for s in PROCEDURAL_ORDER[:-1] if s in stage_counts]
        + unknown_stages
        + (["sin_clasificar"] if "sin_clasificar" in stage_counts else [])
    )
    by_procedural_state = [{"stage": s, "count": stage_counts[s]} for s in ordered_stages]

    lawyer_ids = list(by_lawyer_cases.keys())
    lawyers_by_id = (
        {lw.id: lw for lw in db.query(Lawyer).filter(Lawyer.id.in_(lawyer_ids)).all()}
        if lawyer_ids
        else {}
    )

    by_lawyer = []
    for lid, lcases in by_lawyer_cases.items():
        lsem: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
        lstale = 0
        for c in lcases:
            lsem[_sem_bucket(c.semaforo)] += 1
            if _is_stale(c.last_movement_at):
                lstale += 1
        lw = lawyers_by_id.get(lid)
        by_lawyer.append({
            "rut": normalize_rut(lw.rut) if lw else "",
            "nombre": _clean_nombre(lw.name) if lw else "",
            "case_count": len(lcases),
            "rojo": lsem["rojo"],
            "amarillo": lsem["amarillo"],
            "verde": lsem["verde"],
            "otros": lsem["otros"],
            "stale": lstale,
        })

    by_lawyer.sort(key=lambda x: x["case_count"], reverse=True)

    return {
        "totals": {
            "cases": len(cases),
            "semaforo": sem_totals,
            "stale": stale_total,
            "by_materia": by_materia,
            "by_procedural_state": by_procedural_state,
        },
        "by_lawyer": by_lawyer,
    }


def case_ids_for_abogado(db: Session, account_rut: str, abogado_rut: str) -> set[int]:
    """Return case IDs where abogado_rut is a firm-side abogado (same side as account)."""
    account_rut_norm = normalize_rut(account_rut)
    abogado_rut_norm = normalize_rut(abogado_rut)

    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return set()

    by_case = _abogado_litigantes_by_case(db)
    result: set[int] = set()

    for case_id, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is None:
            continue
        for lit in litigantes:
            if normalize_rut(lit.rut) == abogado_rut_norm and lit.participante in account_side:
                result.add(case_id)
                break

    return result


def admin_dashboard_stats(db: Session, account_rut: str) -> dict:
    """Real Admin dashboard aggregates: sync status, document pipeline, data quality.

    All derived from existing data (no fabricated metrics):
      - sync: freshness of the detail scrape (last/recent checks, pending, stale)
      - documents: GCS download pipeline status breakdown
      - quality: coverage of semaforo / movements / litigantes, and unassigned cases
    """
    from sqlalchemy import func
    from app.models.movement import Movement
    from app.models.document import Document

    empty = {
        "sync": {"last_checked_at": None, "checked_24h": 0, "pending_detail": 0, "stale_30d": 0},
        "documents": {"stored": 0, "pending": 0, "failed": 0, "unavailable": 0},
        "quality": {
            "total_cases": 0, "with_semaforo": 0, "with_movements": 0,
            "with_litigantes": 0, "sin_asignar": 0,
        },
    }

    account_rut_norm = normalize_rut(account_rut)
    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return empty

    now = datetime.utcnow()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_30d = now - timedelta(days=30)

    cases = (
        db.query(Case.id, Case.semaforo, Case.last_detail_checked_at, Case.last_movement_at)
        .filter(Case.lawyer_id == lawyer.id, Case.competencia == "civil")
        .all()
    )
    case_ids = [c.id for c in cases]
    total_cases = len(cases)

    last_checked_at: Optional[datetime] = None
    checked_24h = pending_detail = stale_30d = with_semaforo = 0
    for c in cases:
        lc = c.last_detail_checked_at
        if lc is not None:
            if last_checked_at is None or lc > last_checked_at:
                last_checked_at = lc
            if lc >= cutoff_24h:
                checked_24h += 1
            if c.last_movement_at is None or c.last_movement_at < cutoff_30d:
                stale_30d += 1
        else:
            pending_detail += 1
        if c.semaforo is not None:
            with_semaforo += 1

    doc_counts = {"stored": 0, "pending": 0, "failed": 0, "unavailable": 0}
    with_movements = with_litigantes = 0
    if case_ids:
        for status, cnt in (
            db.query(Document.status, func.count())
            .filter(Document.case_id.in_(case_ids))
            .group_by(Document.status)
            .all()
        ):
            if status in doc_counts:
                doc_counts[status] = int(cnt)
        with_movements = int(
            db.query(func.count(func.distinct(Movement.case_id)))
            .filter(Movement.case_id.in_(case_ids))
            .scalar() or 0
        )
        with_litigantes = int(
            db.query(func.count(func.distinct(CaseLitigante.case_id)))
            .filter(CaseLitigante.case_id.in_(case_ids))
            .scalar() or 0
        )

    # sin_asignar: civil cases with no firm-side abogado resolvable
    by_case = _abogado_litigantes_by_case(db)
    assigned: set[int] = set()
    for cid, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is not None and any(
            lit.participante in account_side for lit in litigantes
        ):
            assigned.add(cid)
    sin_asignar = len(set(case_ids) - assigned)

    return {
        "sync": {
            "last_checked_at": last_checked_at.isoformat() if last_checked_at else None,
            "checked_24h": checked_24h,
            "pending_detail": pending_detail,
            "stale_30d": stale_30d,
        },
        "documents": doc_counts,
        "quality": {
            "total_cases": total_cases,
            "with_semaforo": with_semaforo,
            "with_movements": with_movements,
            "with_litigantes": with_litigantes,
            "sin_asignar": sin_asignar,
        },
    }
