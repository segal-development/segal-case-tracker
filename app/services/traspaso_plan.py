"""Traspaso de cartera — dry-run plan: which cases move and to whom.

Read-only. Picks the origin's non-archived cases by RESOLVED owner (override >
litigante, ``resolved_owner_by_case``) and distributes them deterministically:

* order: rojo, then amarillo, then the rest; nearest plazo first; then case id;
* each case goes to an eligible lawyer whose nivel satisfies its matriz
  (``NIVELES_REQUERIDOS``), minimising (urgent cases received in this plan, only
  for rojo cases; then current load + received in this plan), ties by lowest id;
* no level match -> minimum load among all eligible, warning "nivel no coincide";
  no matriz or provisional one (``sin_detalle``) -> same, warning "sin matriz";
* ``destino_unico_id`` sends everything to one lawyer (warnings still computed).
"""

from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.lawyer import Lawyer
from app.models.liberacion import LIB_PENDIENTE, LiberacionRequest
from app.services.asignacion_engine import NIVELES_REQUERIDOS, ORIGEN_PROVISORIO
from app.services.lawyer_roster import resolved_owner_by_case

PLAZO_PROXIMO_DIAS = 10
WARN_NIVEL = "nivel no coincide"
WARN_SIN_MATRIZ = "sin matriz"
_URGENCIA = {"rojo": 0, "amarillo": 1}


class PlanError(Exception):
    """The plan request is invalid (maps to HTTP 422)."""


def owned_cases(db: Session, lawyer_id: int) -> List[Case]:
    """Non-archived cases whose resolved owner is ``lawyer_id``, ordered by id."""
    ids = [cid for cid, lw in resolved_owner_by_case(db).items() if lw.id == lawyer_id]
    if not ids:
        return []
    return (
        db.query(Case)
        .options(joinedload(Case.court))
        .filter(Case.id.in_(ids), Case.status != "archived")
        .order_by(Case.id)
        .all()
    )


def is_eligible_destination(lawyer: Optional[Lawyer]) -> bool:
    return lawyer is not None and bool(lawyer.is_firm_lawyer) and bool(lawyer.is_active)


def _eligible(db: Session, origen: Lawyer, destino_ids, destino_unico_id) -> List[Lawyer]:
    wanted = [destino_unico_id] if destino_unico_id is not None else list(destino_ids or [])
    if wanted:
        found = {lw.id: lw for lw in db.query(Lawyer).filter(Lawyer.id.in_(wanted)).all()}
        for lid in wanted:
            lw = found.get(lid)
            if lid == origen.id or not is_eligible_destination(lw):
                raise PlanError(f"El destino {lid} no es un abogado activo de la firma distinto del origen")
        return sorted(found.values(), key=lambda lw: lw.id)
    lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.is_firm_lawyer.is_(True), Lawyer.is_active.is_(True), Lawyer.id != origen.id)
        .order_by(Lawyer.id)
        .all()
    )
    if not lawyers:
        raise PlanError("No hay abogados destino elegibles")
    return lawyers


def build_plan(
    db: Session,
    origen_id: int,
    destino_ids: Optional[List[int]] = None,
    destino_unico_id: Optional[int] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    today = today or date.today()
    origen = db.get(Lawyer, origen_id)
    if origen is None:
        raise PlanError("El abogado de origen no existe")
    eligible = _eligible(db, origen, destino_ids, destino_unico_id)

    cases = owned_cases(db, origen.id)
    case_ids = [c.id for c in cases]

    plazo: Dict[int, date] = {}
    plazo_proximo_ids = set()
    horizon = today + timedelta(days=PLAZO_PROXIMO_DIAS)
    sightings: Dict[int, set] = {}
    liberaciones = 0
    if case_ids:
        for cid, due in (
            db.query(CaseDeadline.case_id, CaseDeadline.due_date)
            .filter(CaseDeadline.case_id.in_(case_ids), CaseDeadline.status == "active")
            .all()
        ):
            if cid not in plazo or due < plazo[cid]:
                plazo[cid] = due
            if today <= due <= horizon:
                plazo_proximo_ids.add(cid)
        for cid, lid in db.query(CaseLawyerSource.case_id, CaseLawyerSource.lawyer_id).filter(
            CaseLawyerSource.case_id.in_(case_ids)
        ):
            sightings.setdefault(cid, set()).add(lid)
        liberaciones = (
            db.query(LiberacionRequest.case_id)
            .filter(LiberacionRequest.case_id.in_(case_ids), LiberacionRequest.estado == LIB_PENDIENTE)
            .distinct()
            .count()
        )

    def next_plazo(case: Case) -> Optional[date]:
        return plazo.get(case.id) or case.next_deadline_at

    def solo_origen(case: Case) -> bool:
        return (sightings.get(case.id) or {case.lawyer_id}) == {origen.id}

    load = Counter(lw.id for lw in resolved_owner_by_case(db).values())
    received: Counter = Counter()
    urgent_received: Counter = Counter()
    by_id = {lw.id: lw for lw in eligible}

    ordered = sorted(
        cases,
        key=lambda c: (_URGENCIA.get(c.semaforo, 2), next_plazo(c) or date.max, c.id),
    )
    propuesta = []
    for case in ordered:
        niveles = NIVELES_REQUERIDOS.get(case.matriz or "")
        provisional = case.matriz_origen == ORIGEN_PROVISORIO
        warning = None
        pool = eligible
        if not niveles or provisional:
            warning = WARN_SIN_MATRIZ
        else:
            matching = [lw for lw in eligible if lw.nivel in niveles]
            if matching:
                pool = matching
            else:
                warning = WARN_NIVEL
        if destino_unico_id is not None:
            dest = by_id[destino_unico_id]
            if niveles and not provisional and dest.nivel not in niveles:
                warning = WARN_NIVEL
        else:
            urgent = case.semaforo == "rojo"
            dest = min(
                pool,
                key=lambda lw: (
                    urgent_received[lw.id] if urgent else 0,
                    load[lw.id] + received[lw.id],
                    lw.id,
                ),
            )
        received[dest.id] += 1
        if case.semaforo == "rojo":
            urgent_received[dest.id] += 1
        propuesta.append(
            {
                "case_id": case.id,
                "rol": case.rol,
                "tribunal": case.court.name if case.court else None,
                "matriz": case.matriz,
                "semaforo": case.semaforo,
                "proximo_plazo": next_plazo(case),
                "destino_lawyer_id": dest.id,
                "destino_nombre": dest.name,
                "advertencia": warning,
                "solo_cuenta_origen": solo_origen(case),
            }
        )

    return {
        "origen_lawyer_id": origen.id,
        "resumen": {
            "total": len(cases),
            "por_nivel": dict(Counter(c.matriz or "sin matriz" for c in cases)),
            "por_semaforo": dict(Counter(c.semaforo or "sin semaforo" for c in cases)),
            "plazos_proximos": len(plazo_proximo_ids),
            "liberaciones_pendientes": liberaciones,
            "solo_cuenta_origen": sum(1 for c in cases if solo_origen(c)),
        },
        "propuesta": propuesta,
        "carga": [
            {
                "lawyer_id": lw.id,
                "nombre": lw.name,
                "nivel": lw.nivel,
                "carga_actual": load[lw.id],
                "recibe": received[lw.id],
                "recibe_urgentes": urgent_received[lw.id],
                "carga_final": load[lw.id] + received[lw.id],
            }
            for lw in eligible
        ],
    }
