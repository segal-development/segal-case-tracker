"""External read-only ``GET /causas/{case_id}/detalle`` for the Redaccion system.

Returns the FULL case detail a document drafter needs — everything the internal
``/causas/:id`` + ``/{id}/deadlines`` views surface, EXCEPT documents/PDFs/files
(explicitly out of scope: no documents, no download links, no GCS keys).

The etapa / acción recomendada / plazos blocks reuse the exact internal engines:
  - acción recomendada: ``app.core.decision_rules.resolve_rule`` against the
    denormalized ``Case.recommended_action_code`` (same as ``deadlines.py``).
  - plazos: ``CaseDeadline`` rows + ``DEADLINE_LABELS`` +
    ``count_business_days_remaining`` + advisory abandono/prescripción risk
    (reusing ``deadlines._compute_abandono_risk`` / ``_compute_prescripcion_risk``).
  - etapa: the case's computed ``procedural_state`` resolved to a human label
    plus the canonical juicio-ejecutivo flow with the current stage flagged.

Read-only — no writes (the key's ``last_used_at`` stamp happens in the dep).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.redaccion.deps import require_redaccion_key
from app.api.v1.deadlines import _compute_abandono_risk, _compute_prescripcion_risk
from app.core.deadlines_config import (
    DEADLINE_DISCLAIMER,
    DEADLINE_LABELS,
    ProceduralState,
)
from app.core.decision_rules import RECOMMENDATION_DISCLAIMER, resolve_rule
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.movement import Movement
from app.services.business_days import count_business_days_remaining

router = APIRouter()

_CHILE_TZ = ZoneInfo("America/Santiago")


# ---------------------------------------------------------------------------
# Etapa del procedimiento — human labels + canonical flow.
#
# No server-side ProceduralState label map exists (the internal /deadlines
# endpoint returns the raw code and the frontend renders it), so the labels
# live here, local to this isolated API. The flow is the canonical linear
# juicio-ejecutivo sequence; INDETERMINATE/REBELDE are off-flow states shown
# only via etapa_label + procedural_state.
# ---------------------------------------------------------------------------
ETAPA_LABELS: dict[str, str] = {
    ProceduralState.MANDAMIENTO.value: "Mandamiento de ejecución y embargo",
    ProceduralState.NOTIFICADO.value: "Demanda notificada",
    ProceduralState.EXCEPCIONES.value: "Excepciones opuestas",
    ProceduralState.TRASLADO_EJECUTANTE.value: "Traslado al ejecutante",
    ProceduralState.ADMISIBILIDAD.value: "Admisibilidad de excepciones",
    ProceduralState.AUTO_PRUEBA.value: "Auto de prueba",
    ProceduralState.CITACION_SENTENCIA.value: "Citación para sentencia",
    ProceduralState.SENTENCIA.value: "Sentencia",
    ProceduralState.TERMINADA.value: "Causa terminada",
    ProceduralState.REBELDE.value: "Rebeldía",
    ProceduralState.INDETERMINATE.value: "Etapa indeterminada",
}

# Canonical linear flow of the juicio ejecutivo (defense side).
ETAPA_FLOW: List[str] = [
    ProceduralState.MANDAMIENTO.value,
    ProceduralState.NOTIFICADO.value,
    ProceduralState.EXCEPCIONES.value,
    ProceduralState.TRASLADO_EJECUTANTE.value,
    ProceduralState.ADMISIBILIDAD.value,
    ProceduralState.AUTO_PRUEBA.value,
    ProceduralState.CITACION_SENTENCIA.value,
    ProceduralState.SENTENCIA.value,
    ProceduralState.TERMINADA.value,
]


# ---------------------------------------------------------------------------
# Response schemas — bounded, no documents/files/download links.
# ---------------------------------------------------------------------------


class TribunalInfo(BaseModel):
    nombre: Optional[str] = None
    region: Optional[str] = None


class IdentificacionResponse(BaseModel):
    rol: str
    rit: Optional[str] = None
    caratulado: str
    materia: Optional[str] = None
    procedimiento: Optional[str] = None
    competencia: Optional[str] = None
    tribunal: Optional[TribunalInfo] = None
    ingreso: Optional[datetime] = None
    ultimo_movimiento: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EstadoResponse(BaseModel):
    status: Optional[str] = None
    semaforo: Optional[str] = None
    procedural_state: Optional[str] = None
    en_apremio: bool = False
    abandono_disponible: bool = False
    prescripcion_cumplida: bool = False
    prescripcion_fecha: Optional[date] = None
    titulo_tipo: Optional[str] = None
    titulo_fecha: Optional[date] = None


class EtapaStageResponse(BaseModel):
    code: str
    label: str
    actual: bool


class EtapaResponse(BaseModel):
    procedural_state: Optional[str] = None
    etapa_label: Optional[str] = None
    flow: List[EtapaStageResponse]


class AccionRecomendadaResponse(BaseModel):
    code: str
    action_text: str
    legal_basis: str
    urgency: str
    disclaimer: str


class PlazoItemResponse(BaseModel):
    deadline_type: str
    label: str
    legal_basis: str
    due_date: date
    triggered_at: date
    dias_habiles_remaining: int
    status: str
    is_manual: bool = False
    source_movement_id: Optional[int] = None


class PlazosResponse(BaseModel):
    next_deadline_at: Optional[date] = None
    next_deadline_fatal: bool = False
    activos: List[PlazoItemResponse]
    abandono_risk: str
    prescripcion_risk: str
    disclaimer: str


class ParteResponse(BaseModel):
    participante: str
    rut: str
    persona_type: str
    nombre: str


class NotificacionResponse(BaseModel):
    rol: str
    estado_notif: str
    tipo_notif: str
    fecha_tramite: Optional[datetime] = None
    tipo_participante: str
    nombre: str
    tramite: str
    obs_fallida: Optional[str] = None


class EscritoResponse(BaseModel):
    fecha_ingreso: Optional[datetime] = None
    tipo_escrito: str
    solicitante: str
    tiene_documento: bool
    tiene_anexo: bool
    # NOTE: doc_token intentionally EXCLUDED — it is a download handle and
    # documents are out of scope for this API.


class ExhortoResponse(BaseModel):
    rol_origen: str
    tipo_exhorto: str
    rol_destino: str
    fecha_ordena: Optional[datetime] = None
    fecha_ingreso: Optional[datetime] = None
    tribunal_destino: str
    estado: str


class MovimientoResponse(BaseModel):
    fecha: datetime
    folio: Optional[str] = None
    etapa: Optional[str] = None
    tramite: Optional[str] = None
    descripcion: str


class RedaccionDetalleResponse(BaseModel):
    """The full drafting bundle for one causa — no documents/files."""

    case_id: int
    identificacion: IdentificacionResponse
    estado: EstadoResponse
    etapa: EtapaResponse
    accion_recomendada: Optional[AccionRecomendadaResponse]
    plazos: PlazosResponse
    partes: List[ParteResponse]
    notificaciones: List[NotificacionResponse]
    escritos: List[EscritoResponse]
    exhortos: List[ExhortoResponse]
    historial: List[MovimientoResponse]


@router.get("/causas/{case_id}/detalle", response_model=RedaccionDetalleResponse)
def get_causa_detalle(
    case_id: int,
    db: Session = Depends(get_db),
    _key=Depends(require_redaccion_key),
) -> RedaccionDetalleResponse:
    """Return the full drafting detail for a causa. 404 if it doesn't exist.

    No visibility scope is applied (this is a firm-wide machine integration,
    like the Sysgal API), but documents/files are never exposed.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Case not found"
        )

    # -- identificación ----------------------------------------------------
    tribunal = None
    if case.court is not None:
        tribunal = TribunalInfo(nombre=case.court.name, region=case.court.region)

    identificacion = IdentificacionResponse(
        rol=case.rol,
        rit=case.rit,
        caratulado=f"{case.plaintiff or ''}/{case.defendant or ''}",
        materia=case.matter,
        procedimiento=case.procedure,
        competencia=case.competencia,
        tribunal=tribunal,
        ingreso=case.filed_at,
        ultimo_movimiento=case.last_movement_at,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )

    # -- estado ------------------------------------------------------------
    estado = EstadoResponse(
        status=case.status,
        semaforo=case.semaforo,
        procedural_state=case.procedural_state,
        en_apremio=bool(case.en_apremio),
        abandono_disponible=bool(case.abandono_disponible),
        prescripcion_cumplida=bool(case.prescripcion_cumplida),
        prescripcion_fecha=case.prescripcion_fecha,
        titulo_tipo=case.titulo_tipo,
        titulo_fecha=case.titulo_fecha,
    )

    # -- etapa del procedimiento ------------------------------------------
    current_state = case.procedural_state
    etapa = EtapaResponse(
        procedural_state=current_state,
        etapa_label=ETAPA_LABELS.get(current_state) if current_state else None,
        flow=[
            EtapaStageResponse(
                code=code,
                label=ETAPA_LABELS.get(code, code),
                actual=(code == current_state),
            )
            for code in ETAPA_FLOW
        ],
    )

    # -- acción recomendada (DecisionEngine) ------------------------------
    accion_recomendada: Optional[AccionRecomendadaResponse] = None
    rule = resolve_rule(getattr(case, "recommended_action_code", None))
    if rule is not None:
        accion_recomendada = AccionRecomendadaResponse(
            code=rule.code,
            action_text=rule.action_text,
            legal_basis=rule.legal_basis,
            urgency=rule.urgency.value,
            disclaimer=RECOMMENDATION_DISCLAIMER,
        )

    # -- plazos (DeadlineEngine outputs) ----------------------------------
    today = datetime.now(_CHILE_TZ).date()
    deadline_rows: List[CaseDeadline] = (
        db.query(CaseDeadline)
        .filter(
            CaseDeadline.case_id == case_id,
            CaseDeadline.status != "superseded",
        )
        .order_by(CaseDeadline.due_date.asc())
        .all()
    )
    activos = [
        PlazoItemResponse(
            deadline_type=row.deadline_type,
            label=DEADLINE_LABELS.get(row.deadline_type, row.deadline_type),
            legal_basis=row.legal_basis or "",
            due_date=row.due_date,
            triggered_at=row.triggered_at,
            dias_habiles_remaining=count_business_days_remaining(row.due_date, today),
            status=row.status,
            is_manual=bool(row.is_manual),
            source_movement_id=row.source_movement_id,
        )
        for row in deadline_rows
    ]
    plazos = PlazosResponse(
        next_deadline_at=case.next_deadline_at,
        next_deadline_fatal=bool(case.next_deadline_fatal),
        activos=activos,
        abandono_risk=_compute_abandono_risk(case),
        prescripcion_risk=_compute_prescripcion_risk(case),
        disclaimer=DEADLINE_DISCLAIMER,
    )

    # -- partes / entidades ------------------------------------------------
    litigantes = (
        db.query(CaseLitigante).filter(CaseLitigante.case_id == case_id).all()
    )
    notificaciones = (
        db.query(CaseNotificacion).filter(CaseNotificacion.case_id == case_id).all()
    )
    escritos = db.query(CaseEscrito).filter(CaseEscrito.case_id == case_id).all()
    exhortos = db.query(CaseExhorto).filter(CaseExhorto.case_id == case_id).all()

    # -- historial (movements timeline, NOT documents) --------------------
    movements = (
        db.query(Movement)
        .filter(Movement.case_id == case_id)
        .order_by(Movement.movement_date.desc())
        .all()
    )

    return RedaccionDetalleResponse(
        case_id=case.id,
        identificacion=identificacion,
        estado=estado,
        etapa=etapa,
        accion_recomendada=accion_recomendada,
        plazos=plazos,
        partes=[ParteResponse.model_validate(r, from_attributes=True) for r in litigantes],
        notificaciones=[
            NotificacionResponse.model_validate(r, from_attributes=True)
            for r in notificaciones
        ],
        escritos=[
            EscritoResponse.model_validate(r, from_attributes=True) for r in escritos
        ],
        exhortos=[
            ExhortoResponse.model_validate(r, from_attributes=True) for r in exhortos
        ],
        historial=[
            MovimientoResponse(
                fecha=m.movement_date,
                folio=m.folio,
                etapa=m.stage,
                tramite=m.procedure,
                descripcion=m.description,
            )
            for m in movements
        ],
    )
