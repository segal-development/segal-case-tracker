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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, _resolve_lawyer_id
from app.core.deadlines_config import DEADLINE_DISCLAIMER
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.services.business_days import count_business_days_remaining

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

    deadline_type: str
    label: str
    legal_basis: str
    due_date: date
    triggered_at: date
    dias_habiles_remaining: int
    status: str
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
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/{case_id}/deadlines", response_model=CaseDeadlinesResponse)
async def get_case_deadlines(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Return the procedural deadline timeline for a case.

    Auth required. Case must be owned by the requesting lawyer (404 otherwise).
    The response always includes a mandatory legal disclaimer.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    case = db.query(Case).filter(
        and_(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id,
        )
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
            CaseDeadline.status == "active",
        )
        .order_by(CaseDeadline.due_date.asc())
        .all()
    )

    active_deadlines: list[DeadlineItemResponse] = [
        DeadlineItemResponse(
            deadline_type=row.deadline_type,
            label=_DEADLINE_LABELS.get(row.deadline_type, row.deadline_type),
            legal_basis=row.legal_basis or "",
            due_date=row.due_date,
            triggered_at=row.triggered_at,
            dias_habiles_remaining=count_business_days_remaining(row.due_date, today),
            status=row.status,
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
