"""Firm roster helpers: co-side firm lawyers for the authenticated account."""

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.deadline_engine import _today_chile
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

    # Load civil cases firm-wide — Approach C: Case.lawyer_id is the firm's
    # bookkeeping owner, not the abogado who sees this case, so the case load
    # is NOT restricted to this account's own lawyer_id. Attribution to this
    # account is entirely litigante-derived (abogado_cases above).
    cases = (
        db.query(Case.id, Case.semaforo, Case.last_movement_at, Case.procedure, Case.procedural_state)
        .filter(Case.competencia == "civil")
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
    """Firm-wide dashboard stats spanning EVERY study case, across ALL abogados.

    Used for the auditor role, a transversal role overseeing the whole
    firm's caseload. Under Approach C every ``Case.lawyer_id`` is the firm's
    single bookkeeping owner, so grouping by it (the old behavior) would
    collapse every case into one row. The per-abogado breakdown is instead
    entirely litigante-derived: every AB./AP. litigante firm-wide is grouped
    by their own RUT, independent of ``Case.lawyer_id``.
    """
    cases = (
        db.query(
            Case.id,
            Case.semaforo,
            Case.last_movement_at,
            Case.procedure,
            Case.procedural_state,
        )
        .filter(Case.competencia == "civil")
        .all()
    )
    case_map = {c.id: c for c in cases}

    def _sem_bucket(sem: Optional[str]) -> str:
        return sem if sem in ("rojo", "amarillo", "verde") else "otros"

    stale_cutoff = datetime.utcnow() - timedelta(days=30)

    def _is_stale(lma: Optional[datetime]) -> bool:
        return lma is None or lma < stale_cutoff

    sem_totals: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
    stale_total = 0
    materia_counts: defaultdict[str, int] = defaultdict(int)
    stage_counts: defaultdict[str, int] = defaultdict(int)

    for c in cases:
        sem_totals[_sem_bucket(c.semaforo)] += 1
        if _is_stale(c.last_movement_at):
            stale_total += 1
        materia_counts[c.procedure or "Sin materia"] += 1
        stage = c.procedural_state if c.procedural_state is not None else "sin_clasificar"
        stage_counts[stage] += 1

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

    # Firm-wide per-abogado breakdown: group cases by every AB./AP. litigante
    # RUT appearing on them (no single-account side filter — count every
    # abogado on every case). A lawyer appearing twice on the same case
    # (e.g. patrocinante + AB.DDO) is counted once for that case.
    by_case = _abogado_litigantes_by_case(db)
    abogado_case_ids: dict[str, set[int]] = defaultdict(set)
    abogado_nombre: dict[str, str] = {}
    for case_id, litigantes in by_case.items():
        if case_id not in case_map:
            continue
        for lit in litigantes:
            norm = normalize_rut(lit.rut)
            abogado_case_ids[norm].add(case_id)
            if norm not in abogado_nombre:
                abogado_nombre[norm] = _clean_nombre(lit.nombre)

    by_lawyer = []
    for norm_rut, case_ids in abogado_case_ids.items():
        lsem: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "otros": 0}
        lstale = 0
        for cid in case_ids:
            c = case_map[cid]
            lsem[_sem_bucket(c.semaforo)] += 1
            if _is_stale(c.last_movement_at):
                lstale += 1
        by_lawyer.append({
            "rut": norm_rut,
            "nombre": abogado_nombre.get(norm_rut, ""),
            "case_count": len(case_ids),
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


def firm_risk_board(db: Session, account_rut: str) -> dict:
    """Return the firm-wide RISK BOARD for the auditor/admin dashboard.

    Rolls up the exposure signals already denormalized on ``Case``
    (``semaforo``, ``abandono_disponible``, ``prescripcion_cumplida``,
    ``en_apremio``, ``next_deadline_fatal``/``next_deadline_at``) which today
    are only ever surfaced per-case. Aggregates over ALL active (non-archived)
    firm civil cases, independent of ``Case.lawyer_id`` (Approach C: it is the
    firm's single bookkeeping owner, not the abogado who works the case).

    Per-abogado attribution (``by_lawyer``) mirrors ``firm_dashboard_stats_all``:
    entirely litigante-derived, grouping every AB./AP. abogado-of-record RUT
    appearing on a case, regardless of side. A case with two abogados-of-record
    counts toward both rows.
    """
    # Firm-wide board: access is gated by require_auditor at the endpoint, and
    # the aggregation below is entirely case/litigante-derived — it never uses
    # the requester's account. So, UNLIKE firm_dashboard_stats, we must NOT bail
    # when account_rut isn't a Lawyer row: an auditor's RUT is typically not a
    # litigante-abogado, so bailing returned an all-zero board for exactly the
    # auditor/admin users this board is FOR. account_rut is kept in the
    # signature for API symmetry but is intentionally unused here.
    today = _today_chile()

    cases = (
        db.query(
            Case.id,
            Case.rol,
            Case.plaintiff,
            Case.defendant,
            Case.court_id,
            Case.semaforo,
            Case.abandono_disponible,
            Case.prescripcion_cumplida,
            Case.en_apremio,
            Case.next_deadline_fatal,
            Case.next_deadline_at,
        )
        .filter(Case.competencia == "civil", Case.status != "archived")
        .all()
    )
    case_map = {c.id: c for c in cases}

    def _sem_bucket(sem: Optional[str]) -> str:
        return sem if sem in ("rojo", "amarillo", "verde") else "gris"

    sem_totals: dict[str, int] = {"rojo": 0, "amarillo": 0, "verde": 0, "gris": 0}
    riesgo_totals = {
        "abandono_disponible": 0,
        "prescripcion_cumplida": 0,
        "en_apremio": 0,
        "plazo_fatal_proximo": 0,
        "plazo_fatal_vencido": 0,
    }

    for c in cases:
        sem_totals[_sem_bucket(c.semaforo)] += 1
        if c.abandono_disponible:
            riesgo_totals["abandono_disponible"] += 1
        if c.prescripcion_cumplida:
            riesgo_totals["prescripcion_cumplida"] += 1
        if c.en_apremio:
            riesgo_totals["en_apremio"] += 1
        if c.next_deadline_fatal and c.next_deadline_at is not None:
            if c.next_deadline_at >= today:
                riesgo_totals["plazo_fatal_proximo"] += 1
            else:
                riesgo_totals["plazo_fatal_vencido"] += 1

    # by_lawyer / top_critical abogado names: firm-wide litigante attribution
    # (mirrors firm_dashboard_stats_all — no single-account side filter, every
    # AB./AP. abogado-of-record RUT on a case counts toward that case).
    by_case = _abogado_litigantes_by_case(db)
    abogado_case_ids: dict[str, set[int]] = defaultdict(set)
    abogado_nombre: dict[str, str] = {}
    case_abogado_names: dict[int, list[str]] = defaultdict(list)
    for case_id, litigantes in by_case.items():
        if case_id not in case_map:
            continue
        for lit in litigantes:
            norm = normalize_rut(lit.rut)
            abogado_case_ids[norm].add(case_id)
            nombre = _clean_nombre(lit.nombre)
            if norm not in abogado_nombre:
                abogado_nombre[norm] = nombre
            if nombre and nombre not in case_abogado_names[case_id]:
                case_abogado_names[case_id].append(nombre)

    by_lawyer = []
    for norm_rut, case_ids in abogado_case_ids.items():
        rojo = abandono = apremio = 0
        for cid in case_ids:
            c = case_map[cid]
            if c.semaforo == "rojo":
                rojo += 1
            if c.abandono_disponible:
                abandono += 1
            if c.en_apremio:
                apremio += 1
        by_lawyer.append({
            "rut": norm_rut,
            "nombre": abogado_nombre.get(norm_rut, ""),
            "rojo": rojo,
            "abandono_disponible": abandono,
            "en_apremio": apremio,
            "total": len(case_ids),
        })
    by_lawyer.sort(key=lambda x: x["rojo"], reverse=True)

    # top_critical: rojo first, then soonest next_deadline_at (cases without
    # one sort last within their group), fatal before non-fatal as tiebreak.
    court_ids = {c.court_id for c in cases if c.court_id is not None}
    court_names = {}
    if court_ids:
        court_names = {
            court.id: court.name
            for court in db.query(Court).filter(Court.id.in_(court_ids)).all()
        }

    def _sort_key(c) -> tuple:
        has_deadline = c.next_deadline_at is not None
        return (
            0 if c.semaforo == "rojo" else 1,
            0 if has_deadline else 1,
            c.next_deadline_at if has_deadline else date.max,
            0 if c.next_deadline_fatal else 1,
        )

    ordered_cases = sorted(cases, key=_sort_key)[:15]

    top_critical = [
        {
            "case_id": c.id,
            "rol": c.rol,
            "caratulado": f"{c.plaintiff or ''}/{c.defendant or ''}",
            "tribunal": court_names.get(c.court_id),
            "abogado_nombre": ", ".join(case_abogado_names.get(c.id, [])),
            "semaforo": c.semaforo,
            "next_deadline_at": c.next_deadline_at,
            "next_deadline_fatal": c.next_deadline_fatal,
        }
        for c in ordered_cases
    ]

    return {
        "total": len(cases),
        "semaforo": sem_totals,
        "riesgo": riesgo_totals,
        "by_lawyer": by_lawyer,
        "top_critical": top_critical,
    }


def case_ids_for_abogado(db: Session, account_rut: str, abogado_rut: str) -> set[int]:
    """Return case IDs where ``abogado_rut`` is an abogado-of-record litigante.

    Firm-wide attribution (Approach C): an abogado sees every case where they
    are personally an abogado-of-record (``participante`` in ``ALL_ABOGADO``),
    regardless of which side that is, and regardless of whether the VIEWING
    ``account_rut`` is also a litigante on that same case. ``account_rut`` is
    kept in the signature (existing callers pass it, e.g. to authorize a
    self-lookup or an admin viewing another firm lawyer's caseload) but it no
    longer gates results — only ``abogado_rut``'s own litigante rows decide
    membership. This intentionally replaces the previous "same side as
    account" gating, which hid legitimate cases whenever the viewing account
    was not itself a litigante on them.
    """
    abogado_rut_norm = normalize_rut(abogado_rut)

    return {
        case_id
        for case_id, litigantes in _abogado_litigantes_by_case(db).items()
        if any(
            normalize_rut(lit.rut) == abogado_rut_norm and lit.participante in ALL_ABOGADO
            for lit in litigantes
        )
    }


def acting_lawyer_is_case_abogado(db: Session, case: Case, acting_rut: str) -> bool:
    """True iff ``acting_rut`` (normalized) is an AB./AP. abogado-of-record
    litigante on ``case``.

    Used to authorize owner-only mutations (e.g. ``archive_case``) under
    Approach C, where ``Case.lawyer_id`` no longer distinguishes which
    lawyer owns/sees a case (it is the firm's single bookkeeping owner).
    """
    acting_norm = normalize_rut(acting_rut)
    litigantes = (
        db.query(CaseLitigante)
        .filter(
            CaseLitigante.case_id == case.id,
            CaseLitigante.participante.in_(list(ALL_ABOGADO)),
        )
        .all()
    )
    return any(normalize_rut(lit.rut) == acting_norm for lit in litigantes)


def resolve_case_alert_recipients(db: Session, case: Case) -> list[Lawyer]:
    """Resolve the internal-lawyer recipient set for alerts/webhooks on ``case``.

    From ``case``'s AB./AP. abogado-of-record litigantes, normalize each RUT
    and match to an internal ``Lawyer`` row. Opposing/external counsel (no
    matching ``Lawyer`` row) are naturally excluded — they simply have
    nothing to match. De-dupes by ``lawyer.id`` so a lawyer appearing under
    multiple litigante rows (e.g. patrocinante + AB.DDO) yields one delivery.

    Used by ``SyncService.sync_movements`` (movement-alert fan-out) and
    ``deadlines.py`` (deadline_audit/deadline_added fan-out) per ADR-005.
    Callers apply their own empty-recipients fallback (e.g. the firm lawyer).
    """
    litigantes = (
        db.query(CaseLitigante)
        .filter(
            CaseLitigante.case_id == case.id,
            CaseLitigante.participante.in_(list(ALL_ABOGADO)),
        )
        .all()
    )
    if not litigantes:
        return []

    ruts = {normalize_rut(lit.rut) for lit in litigantes if lit.rut}
    if not ruts:
        return []

    lawyers = db.query(Lawyer).filter(Lawyer.rut.in_(ruts)).all()
    seen: set = set()
    result: list[Lawyer] = []
    for lawyer in lawyers:
        if lawyer.id in seen:
            continue
        seen.add(lawyer.id)
        result.append(lawyer)
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

    # Firm-wide civil case load — Approach C: Case.lawyer_id is the firm's
    # bookkeeping owner, not this account specifically, so admin quality/sync
    # metrics span every civil case regardless of which lawyer synced it.
    cases = (
        db.query(Case.id, Case.semaforo, Case.last_detail_checked_at, Case.last_movement_at)
        .filter(Case.competencia == "civil")
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
