"""Liberación de causa endpoints — dual sign-off semáforo override.

A lawyer requests moving a case's semáforo (target + motivo). It only applies
under CROSS-CONTROL: an auditor (role=auditor) AND the dirección (role=admin)
must both authorize. On apply, a manual override is written on the Case (the
DeadlineEngine honors it until a newer movement supersedes it).
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.models.liberacion import (
    LiberacionRequest,
    LIB_APLICADO,
    LIB_PENDIENTE,
    LIB_RECHAZADO,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_SEMAFORO_VALUES = {"rojo", "amarillo", "verde"}
_OVERSIGHT_ROLES = {"auditor", "admin"}


class CrearLiberacionBody(BaseModel):
    case_id: int
    target_semaforo: str  # rojo | amarillo | verde
    motivo: Optional[str] = None


class RechazoBody(BaseModel):
    motivo: Optional[str] = None


class LiberacionResponse(BaseModel):
    id: int
    case_id: int
    rol: Optional[str] = None
    caratulado: Optional[str] = None
    semaforo_actual: Optional[str] = None
    target_semaforo: str
    motivo: Optional[str] = None
    estado: str
    requested_by_name: Optional[str] = None
    auditor_ok: bool
    auditor_aprobado_by_name: Optional[str] = None
    direccion_ok: bool
    direccion_aprobado_by_name: Optional[str] = None
    rechazo_motivo: Optional[str] = None


def _resolve_lawyer(db: Session, current_lawyer: dict) -> Optional[Lawyer]:
    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if sub is None:
        return None
    if isinstance(sub, int) or (isinstance(sub, str) and str(sub).isdigit()):
        return db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    return db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()


def _to_response(db: Session, r: LiberacionRequest) -> LiberacionResponse:
    case = r.case or db.query(Case).filter(Case.id == r.case_id).first()
    return LiberacionResponse(
        id=r.id,
        case_id=r.case_id,
        rol=case.rol if case else None,
        caratulado=f"{case.plaintiff or ''}/{case.defendant or ''}" if case else None,
        semaforo_actual=case.semaforo if case else None,
        target_semaforo=r.target_semaforo,
        motivo=r.motivo,
        estado=r.estado,
        requested_by_name=r.requested_by_name,
        auditor_ok=r.auditor_ok,
        auditor_aprobado_by_name=r.auditor_aprobado_by_name,
        direccion_ok=r.direccion_ok,
        direccion_aprobado_by_name=r.direccion_aprobado_by_name,
        rechazo_motivo=r.rechazo_motivo,
    )


def _maybe_apply(db: Session, r: LiberacionRequest) -> None:
    """When both sign-offs are present, apply the override to the case."""
    if not (r.auditor_ok and r.direccion_ok):
        return
    case = db.query(Case).filter(Case.id == r.case_id).first()
    if case is None:
        return
    now = datetime.utcnow()
    r.estado = LIB_APLICADO
    r.aplicado_at = now
    case.semaforo_override = r.target_semaforo
    case.semaforo_override_at = now
    case.semaforo_override_by = " + ".join(
        n for n in (r.auditor_aprobado_by_name, r.direccion_aprobado_by_name) if n
    )
    case.semaforo = r.target_semaforo  # reflect immediately


@router.post("", response_model=LiberacionResponse, status_code=status.HTTP_201_CREATED)
async def crear_liberacion(
    body: CrearLiberacionBody,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Request moving a case's semáforo (needs later auditor + dirección sign-off)."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")
    if body.target_semaforo not in _SEMAFORO_VALUES:
        raise HTTPException(status_code=400, detail="Estado destino inválido (rojo/amarillo/verde)")
    case = db.query(Case).filter(Case.id == body.case_id).first()
    if case is None:
        raise HTTPException(status_code=404, detail="Causa no encontrada")

    existing = (
        db.query(LiberacionRequest)
        .filter(LiberacionRequest.case_id == body.case_id, LiberacionRequest.estado == LIB_PENDIENTE)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Ya hay una solicitud de liberación pendiente para esta causa")

    req = LiberacionRequest(
        case_id=body.case_id,
        requested_by_rut=actor.rut,
        requested_by_name=actor.name,
        target_semaforo=body.target_semaforo,
        motivo=body.motivo,
        estado=LIB_PENDIENTE,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return _to_response(db, req)


@router.get("", response_model=List[LiberacionResponse])
async def list_liberaciones(
    estado: str = Query(LIB_PENDIENTE),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """List liberación requests — for the oversight roles (auditor / dirección)."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None or actor.role not in _OVERSIGHT_ROLES:
        raise HTTPException(status_code=403, detail="Solo auditor o dirección")
    q = db.query(LiberacionRequest)
    if estado:
        q = q.filter(LiberacionRequest.estado == estado)
    return [_to_response(db, r) for r in q.order_by(LiberacionRequest.created_at.desc()).all()]


@router.get("/pendientes/count")
async def count_pendientes(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Count of pending requests — drives the header badge (oversight roles only)."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None or actor.role not in _OVERSIGHT_ROLES:
        return {"count": 0}
    n = db.query(LiberacionRequest).filter(LiberacionRequest.estado == LIB_PENDIENTE).count()
    return {"count": n}


@router.post("/{req_id}/aprobar", response_model=LiberacionResponse)
async def aprobar_liberacion(
    req_id: int,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Sign off a liberación. The actor's ROLE decides which side of the dual
    control it fills: role=auditor → auditor sign-off; role=admin → dirección
    sign-off. Applies the move only once BOTH are present."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None or actor.role not in _OVERSIGHT_ROLES:
        raise HTTPException(status_code=403, detail="Solo auditor o dirección pueden autorizar")
    r = db.query(LiberacionRequest).filter(LiberacionRequest.id == req_id).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if r.estado != LIB_PENDIENTE:
        raise HTTPException(status_code=409, detail=f"La solicitud ya está {r.estado}")

    now = datetime.utcnow()
    if actor.role == "auditor":
        if r.auditor_ok:
            raise HTTPException(status_code=409, detail="El auditor ya autorizó")
        r.auditor_aprobado_by_rut = actor.rut
        r.auditor_aprobado_by_name = actor.name
        r.auditor_aprobado_at = now
    else:  # admin = dirección
        if r.direccion_ok:
            raise HTTPException(status_code=409, detail="La dirección ya autorizó")
        r.direccion_aprobado_by_rut = actor.rut
        r.direccion_aprobado_by_name = actor.name
        r.direccion_aprobado_at = now

    _maybe_apply(db, r)
    db.commit()
    db.refresh(r)
    return _to_response(db, r)


@router.post("/{req_id}/rechazar", response_model=LiberacionResponse)
async def rechazar_liberacion(
    req_id: int,
    body: RechazoBody,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Reject a liberación (either oversight role can)."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None or actor.role not in _OVERSIGHT_ROLES:
        raise HTTPException(status_code=403, detail="Solo auditor o dirección")
    r = db.query(LiberacionRequest).filter(LiberacionRequest.id == req_id).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if r.estado != LIB_PENDIENTE:
        raise HTTPException(status_code=409, detail=f"La solicitud ya está {r.estado}")
    r.estado = LIB_RECHAZADO
    r.rechazado_by_rut = actor.rut
    r.rechazado_by_name = actor.name
    r.rechazo_motivo = body.motivo
    db.commit()
    db.refresh(r)
    return _to_response(db, r)
