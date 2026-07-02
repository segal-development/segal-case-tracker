"""GET /cases/{case_id}/deadlines — procedural deadline timeline for a civil case.

Returns the case's ProceduralState, semáforo color, the full deadline timeline
(each row: type, label, legal_basis, due_date, triggered_at,
dias_habiles_remaining, status, source_movement_id), the próxima acción, and
advisory abandono / prescripción risk flags.

ALWAYS includes DEADLINE_DISCLAIMER — mandatory legal advisory notice.
Auth required; lawyer-scoped (404 when case not owned by caller).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import nullslast

from app.api.deps import (
    get_current_lawyer,
    get_db,
    require_auditor,
    resolve_case_scope,
    apply_case_scope,
)
from app.core.deadlines_config import DEADLINE_DISCLAIMER, DeadlineType
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer
from app.services.business_days import count_business_days_remaining, add_business_days

from zoneinfo import ZoneInfo

router = APIRouter()

_CHILE_TZ = ZoneInfo("America/Santiago")

# ---------------------------------------------------------------------------
# Human-readable labels per deadline type (display copy, NOT legal text)
# ---------------------------------------------------------------------------

_DEADLINE_LABELS: dict[str, str] = {
    "excepciones_8d": "Plazo para oponer excepciones",
    "traslado_ejecutante_4d": "Traslado al ejecutante",
    "termino_probatorio_10d": "Término probatorio",
    "lista_testigos_2d": "Lista de testigos",
    "observaciones_prueba_6d": "Observaciones a la prueba",
    "sentencia_10d": "Plazo para dictar sentencia",
    "apelacion_5d": "Plazo de apelación",
}

_DEADLINE_ACTIONS: dict[str, str] = {
    "excepciones_8d": "Vence el plazo para que el ejecutado oponga excepciones (art. 459 CPC).",
    "traslado_ejecutante_4d": "Vence el traslado al ejecutante para responder a las excepciones (art. 466 CPC).",
    "termino_probatorio_10d": "Vence el término probatorio (art. 468 CPC).",
    "lista_testigos_2d": "Vence el plazo para presentar lista de testigos (art. 468 CPC).",
    "observaciones_prueba_6d": "Vence el plazo para hacer observaciones a la prueba (art. 469 CPC).",
    "sentencia_10d": "Vence el plazo para dictar sentencia definitiva (arts. 162/470 CPC).",
    "apelacion_5d": "Vence el plazo para interponer apelación (arts. 187/475 CPC).",
}


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class DeadlineItemResponse(BaseModel):
    """One procedural deadline in the timeline."""

    id: int
    deadline_type: str
    label: str
    legal_basis: str
    due_date: date
    triggered_at: date
    dias_habiles_remaining: int
    status: str
    is_manual: bool = False
    source_movement_id: Optional[int] = None

    class Config:
        from_attributes = True


class ProximaAccionResponse(BaseModel):
    """The nearest active deadline + the action it represents."""

    deadline_type: str
    label: str
    due_date: date
    dias_habiles_remaining: int
    description: str


class CaseDeadlinesResponse(BaseModel):
    """Full deadline response for a case."""

    case_id: int
    procedural_state: Optional[str]
    semaforo: Optional[str]
    active_deadlines: List[DeadlineItemResponse]
    proxima_accion: Optional[ProximaAccionResponse]
    abandono_risk: str
    prescripcion_risk: str
    disclaimer: str


# ---------------------------------------------------------------------------
# Risk flag helpers
# ---------------------------------------------------------------------------

# Thresholds in days
_ABANDONO_APPROACHING_DAYS = 135   # ≈ 4.5 months
_ABANDONO_PRESUMIBLE_DAYS = 180    # ≈ 6 months (art. 152 CPC)
_ABANDONO_REBELDE_APPROACHING_DAYS = 912  # ≈ 2.5 years (art. 153 inc. 2 CPC)
_ABANDONO_REBELDE_PRESUMIBLE_DAYS = 1095  # ≈ 3 years

_PRESCRIPCION_APPROACHING_DAYS = 912   # ≈ 2.5 years (art. 2515 CC)
_PRESCRIPCION_AT_RISK_DAYS = 1095      # ≈ 3 years


def _compute_abandono_risk(case: Case) -> str:
    """Compute abandono_risk advisory flag from Case.last_movement_at.

    Art. 152 CPC: 6-month window (general).
    Art. 153 inc. 2 CPC: 3-year window for REBELDE cases.
    Returns: 'none' | 'approaching' | 'presumible'
    """
    if case.last_movement_at is None:
        return "none"

    today = datetime.now(_CHILE_TZ)
    elapsed_days = (today - _as_utc(case.last_movement_at)).days

    is_rebelde = (case.procedural_state or "").lower() == "rebelde"

    if is_rebelde:
        if elapsed_days >= _ABANDONO_REBELDE_PRESUMIBLE_DAYS:
            return "presumible"
        elif elapsed_days >= _ABANDONO_REBELDE_APPROACHING_DAYS:
            return "approaching"
        else:
            return "none"
    else:
        if elapsed_days >= _ABANDONO_PRESUMIBLE_DAYS:
            return "presumible"
        elif elapsed_days >= _ABANDONO_APPROACHING_DAYS:
            return "approaching"
        else:
            return "none"


def _compute_prescripcion_risk(case: Case) -> str:
    """Compute prescripcion_risk advisory flag from Case.filed_at.

    Art. 2515 CC: 3-year acción ejecutiva (pagaré: art. 98 Ley 18.092 = 1y,
    deferred to Slice B via Case.instrument_type).
    Returns: 'none' | 'approaching' | 'at_risk'
    """
    if case.filed_at is None:
        return "none"

    today = datetime.now(_CHILE_TZ)
    elapsed_days = (today - _as_utc(case.filed_at)).days

    if elapsed_days >= _PRESCRIPCION_AT_RISK_DAYS:
        return "at_risk"
    elif elapsed_days >= _PRESCRIPCION_APPROACHING_DAYS:
        return "approaching"
    else:
        return "none"


def _as_utc(dt: datetime) -> datetime:
    """Return an offset-aware datetime in Chile TZ from a naive or aware dt."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CHILE_TZ)
    return dt.astimezone(_CHILE_TZ)


# ---------------------------------------------------------------------------
# Auditor schemas
# ---------------------------------------------------------------------------


class CatalogItemResponse(BaseModel):
    value: str
    label: str
    dias: int
    legal_basis: str
    is_fatal: bool


class DeadlineStatusUpdateBody(BaseModel):
    status: str  # "cumplido" | "no_cumplido" | "active"


class DeadlineResponse(BaseModel):
    id: int
    case_id: int
    deadline_type: str
    legal_basis: Optional[str]
    due_date: date
    triggered_at: date
    status: str
    is_manual: bool
    marked_by: Optional[int] = None
    marked_at: Optional[datetime] = None
    source_movement_id: Optional[int] = None
    computed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ManualDeadlineBody(BaseModel):
    deadline_type: str
    # The date the deadline starts running. due_date is derived =
    # start_date + the type's N días hábiles (CL holidays-aware).
    start_date: date


class AuditedDeadlineResponse(BaseModel):
    """One audited (cumplido / no_cumplido) deadline, enriched with case info."""

    id: int
    case_id: int
    rol: str
    caratula: str
    deadline_type: str
    label: str
    legal_basis: Optional[str]
    due_date: date
    triggered_at: date
    status: str
    is_manual: bool
    marked_at: Optional[datetime]
    abogado_nombre: Optional[str]


# ---------------------------------------------------------------------------
# Auditor endpoints
# ---------------------------------------------------------------------------


@router.get("/deadlines/catalog", response_model=list[CatalogItemResponse])
async def get_deadlines_catalog(
    _current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return the full DeadlineType catalog for the auditor manual-deadline modal."""
    return [
        CatalogItemResponse(
            value=dt.value,
            label=_DEADLINE_LABELS.get(dt.value, dt.value),
            dias=dt.dias_habiles,
            legal_basis=dt.legal_basis,
            is_fatal=dt.is_fatal,
        )
        for dt in DeadlineType
    ]


class ComputeDueResponse(BaseModel):
    deadline_type: str
    label: str
    legal_basis: str
    dias_habiles: int
    is_fatal: bool
    start_date: date
    due_date: date


@router.get("/deadlines/compute-due", response_model=ComputeDueResponse)
async def compute_due_date(
    deadline_type: str = Query(..., description="Catalog deadline type value"),
    start_date: date = Query(..., description="Date the deadline starts running"),
    _current_lawyer: dict = Depends(get_current_lawyer),
):
    """Preview a manual deadline's due_date = start_date + N días hábiles (CL holidays-aware)."""
    valid_values = {dt.value for dt in DeadlineType}
    if deadline_type not in valid_values:
        raise HTTPException(
            status_code=422,
            detail=f"deadline_type must be one of {sorted(valid_values)}",
        )
    dt = DeadlineType(deadline_type)
    return ComputeDueResponse(
        deadline_type=dt.value,
        label=_DEADLINE_LABELS.get(dt.value, dt.value),
        legal_basis=dt.legal_basis,
        dias_habiles=dt.dias_habiles,
        is_fatal=dt.is_fatal,
        start_date=start_date,
        due_date=add_business_days(start_date, dt.dias_habiles),
    )


@router.get("/deadlines/audited", response_model=list[AuditedDeadlineResponse])
async def list_audited_deadlines(
    status: Optional[str] = Query(
        default=None,
        description="Filter by audited status. Omitted → both cumplido and no_cumplido.",
    ),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List audited deadlines (cumplido / no_cumplido) for the caller's cases.

    Scoped via ``resolve_case_scope``: the auditor role sees the whole
    firm's caseload across ALL lawyers; every other role stays scoped to
    its own cases. Optional ``status`` narrows to a single audited status.
    """
    audited_statuses = {"cumplido", "no_cumplido"}
    if status is not None and status not in audited_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(audited_statuses)}",
        )

    scope = resolve_case_scope(db, current_lawyer)
    status_filter = {status} if status is not None else audited_statuses

    rows_query = (
        db.query(CaseDeadline, Case)
        .join(Case, CaseDeadline.case_id == Case.id)
        .filter(CaseDeadline.status.in_(status_filter))
    )
    rows = (
        apply_case_scope(rows_query, scope)
        .order_by(
            nullslast(CaseDeadline.marked_at.desc()),
            CaseDeadline.due_date.desc(),
        )
        .all()
    )

    # Resolve lawyer names once (small cache) for abogado_nombre.
    name_cache: dict[int, Optional[str]] = {}

    def _lawyer_name(lid: Optional[int]) -> Optional[str]:
        if lid is None:
            return None
        if lid not in name_cache:
            lw = db.get(Lawyer, lid)
            name_cache[lid] = lw.name if lw else None
        return name_cache[lid]

    return [
        AuditedDeadlineResponse(
            id=dl.id,
            case_id=case.id,
            rol=case.rol or "",
            caratula=f"{case.plaintiff or ''}/{case.defendant or ''}",
            deadline_type=dl.deadline_type,
            label=_DEADLINE_LABELS.get(dl.deadline_type, dl.deadline_type),
            legal_basis=dl.legal_basis,
            due_date=dl.due_date,
            triggered_at=dl.triggered_at,
            status=dl.status,
            is_manual=dl.is_manual,
            marked_at=dl.marked_at,
            abogado_nombre=_lawyer_name(case.lawyer_id),
        )
        for dl, case in rows
    ]


@router.put("/{case_id}/deadlines/{deadline_id}/status", response_model=DeadlineResponse)
async def update_deadline_status(
    case_id: int,
    deadline_id: int,
    body: DeadlineStatusUpdateBody,
    auditor_rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Mark a deadline as cumplido / no_cumplido / active (auditor or admin only)."""
    allowed_statuses = {"cumplido", "no_cumplido", "active"}
    if body.status not in allowed_statuses:
        raise HTTPException(status_code=422, detail=f"status must be one of {allowed_statuses}")

    deadline = (
        db.query(CaseDeadline)
        .filter(
            CaseDeadline.id == deadline_id,
            CaseDeadline.case_id == case_id,
        )
        .first()
    )
    if not deadline:
        raise HTTPException(status_code=404, detail="Deadline not found")

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Resolve auditor's lawyer id from the RUT returned by require_auditor
    from app.models.lawyer import Lawyer

    auditor = db.query(Lawyer).filter(Lawyer.rut == auditor_rut).first()
    auditor_id = auditor.id if auditor else None

    deadline.status = body.status
    deadline.marked_by = auditor_id
    deadline.marked_at = datetime.utcnow()

    # Notify the case's owning lawyer
    alert = Alert(
        lawyer_id=case.lawyer_id,
        case_id=case.id,
        type="deadline_audit",
        title=f"Plazo {body.status} · {case.rol}",
        message=f"El auditor marcó '{deadline.deadline_type}' como {body.status}.",
        created_at=datetime.utcnow(),
    )
    db.add(alert)
    db.commit()
    db.refresh(deadline)
    return deadline


@router.post("/{case_id}/deadlines", response_model=DeadlineResponse, status_code=201)
async def create_manual_deadline(
    case_id: int,
    body: ManualDeadlineBody,
    auditor_rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Add a manual deadline to a case from the catalog (auditor or admin only)."""
    # Validate deadline_type is a known catalog value
    valid_values = {dt.value for dt in DeadlineType}
    if body.deadline_type not in valid_values:
        raise HTTPException(
            status_code=422,
            detail=f"deadline_type must be one of {sorted(valid_values)}",
        )

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    dt = DeadlineType(body.deadline_type)
    # The deadline runs from start_date; due_date = start_date + N días hábiles
    # (CL holidays-aware) — same arithmetic the engine uses for automatic deadlines.
    triggered_at = body.start_date
    due_date = add_business_days(triggered_at, dt.dias_habiles)

    new_deadline = CaseDeadline(
        case_id=case_id,
        deadline_type=dt.value,
        legal_basis=dt.legal_basis,
        due_date=due_date,
        triggered_at=triggered_at,
        status="active",
        is_manual=True,
        computed_at=None,
    )
    db.add(new_deadline)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Unique constraint clash: (case_id, deadline_type, triggered_at=today).
        # Strategy: return 409 — auditor must pick a different type or wait until tomorrow.
        raise HTTPException(
            status_code=409,
            detail=(
                f"A deadline of type '{dt.value}' starting on {triggered_at} already exists for this case. "
                "Change the deadline_type or the start date."
            ),
        )

    # Notify the case's owning lawyer
    alert = Alert(
        lawyer_id=case.lawyer_id,
        case_id=case.id,
        type="deadline_added",
        title=f"Nuevo plazo · {case.rol}",
        message=f"El auditor agregó un plazo manual '{dt.value}' con vencimiento {due_date}.",
        created_at=datetime.utcnow(),
    )
    db.add(alert)
    db.commit()
    db.refresh(new_deadline)
    return new_deadline


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/{case_id}/deadlines", response_model=CaseDeadlinesResponse)
async def get_case_deadlines(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Return the procedural deadline timeline for a case.

    Auth required. Case must be visible to the caller: owned by the
    requesting lawyer, or any study case for the auditor role (404
    otherwise). The response always includes a mandatory legal disclaimer.
    """
    scope = resolve_case_scope(db, current_lawyer)

    case = apply_case_scope(
        db.query(Case).filter(Case.id == case_id), scope
    ).first()

    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    today = datetime.now(_CHILE_TZ).date()

    # Fetch active deadline rows ordered by due_date ASC
    deadline_rows: list[CaseDeadline] = (
        db.query(CaseDeadline)
        .filter(
            CaseDeadline.case_id == case_id,
            # active + audited (cumplido/no_cumplido) so the lawyer SEES audited
            # deadlines; only "superseded" rows are hidden.
            CaseDeadline.status != "superseded",
        )
        .order_by(CaseDeadline.due_date.asc())
        .all()
    )

    active_deadlines: list[DeadlineItemResponse] = [
        DeadlineItemResponse(
            id=row.id,
            deadline_type=row.deadline_type,
            label=_DEADLINE_LABELS.get(row.deadline_type, row.deadline_type),
            legal_basis=row.legal_basis or "",
            due_date=row.due_date,
            triggered_at=row.triggered_at,
            dias_habiles_remaining=count_business_days_remaining(row.due_date, today),
            status=row.status,
            is_manual=row.is_manual,
            source_movement_id=row.source_movement_id,
        )
        for row in deadline_rows
    ]

    # Próxima acción = nearest active deadline (first in the ASC-ordered list)
    proxima_accion: Optional[ProximaAccionResponse] = None
    if active_deadlines:
        nearest = active_deadlines[0]
        proxima_accion = ProximaAccionResponse(
            deadline_type=nearest.deadline_type,
            label=nearest.label,
            due_date=nearest.due_date,
            dias_habiles_remaining=nearest.dias_habiles_remaining,
            description=_DEADLINE_ACTIONS.get(
                nearest.deadline_type,
                f"Vence plazo {nearest.label}.",
            ),
        )

    return CaseDeadlinesResponse(
        case_id=case.id,
        procedural_state=case.procedural_state,
        semaforo=case.semaforo,
        active_deadlines=active_deadlines,
        proxima_accion=proxima_accion,
        abandono_risk=_compute_abandono_risk(case),
        prescripcion_risk=_compute_prescripcion_risk(case),
        disclaimer=DEADLINE_DISCLAIMER,
    )
