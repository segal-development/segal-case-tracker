"""External ``POST /presentaciones`` + ``GET /presentaciones/{id}`` for the GEDOC system.

Queues a filing (demanda/escrito) to be presented at the PJUD's OJV and reports
its lifecycle state. Slice 1 is the API contract + persistence ONLY: the created
row sits at ``estado="en_cola"`` and nothing processes it — NO browser
automation, NO PJUD/Playwright calls, no worker. Zero writes to any external
system.

The ``X-API-Key`` stamp on ``last_used_at`` (in the dep) is the only side effect
beyond persisting the queued presentación itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.presentacion.deps import require_presentacion_key
from app.models.presentacion import (
    PRES_CARGADA_PENDIENTE,
    PRES_EN_COLA,
    PRES_ENVIO_CONFIRMADO,
    Presentacion,
)

# prefix="/presentaciones" so the full path (mounted at /api/presentacion/v1)
# resolves to POST /api/presentacion/v1/presentaciones and
# GET /api/presentacion/v1/presentaciones/{id}.
router = APIRouter(prefix="/presentaciones")

_TIPOS_GESTION = {"demanda", "escrito"}
_MODOS = {"semiauto", "auto"}


# ---------------------------------------------------------------------------
# Request schemas — the filing GEDOC wants queued.
# ---------------------------------------------------------------------------


class DocumentoAnexo(BaseModel):
    url: str
    referencia: str
    tipo: Optional[str] = None
    cantidad: int = 1
    original_papel: bool = False


class DocumentoPrincipal(BaseModel):
    url: str
    referencia: str = "Demanda"


class Litigante(BaseModel):
    # Permissive: GEDOC may send partial party data. Nothing is required.
    tipo_sujeto: Optional[str] = None
    rut: Optional[str] = None
    tipo_persona: Optional[str] = None
    nombres: Optional[str] = None
    apellidos: Optional[str] = None


class PresentacionCreate(BaseModel):
    idempotency_key: str
    tipo_gestion: str  # "demanda" | "escrito"
    credential_ref: str  # RUT of the OJV account that presents
    modo: str = "semiauto"  # "semiauto" | "auto"
    competencia: Optional[str] = None
    corte: Optional[str] = None
    tribunal: Optional[str] = None
    rol: Optional[str] = None
    anio: Optional[str] = None
    procedimiento: Optional[str] = None
    materia: Optional[str] = None
    dirigido_a_rut: Optional[str] = None
    litigantes: List[Litigante] = []
    documento_principal: DocumentoPrincipal
    documentos: List[DocumentoAnexo] = []

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("idempotency_key must be a non-empty string")
        return v

    @field_validator("tipo_gestion")
    @classmethod
    def _check_tipo_gestion(cls, v: str) -> str:
        if v not in _TIPOS_GESTION:
            raise ValueError(f"tipo_gestion must be one of {sorted(_TIPOS_GESTION)}")
        return v

    @field_validator("modo")
    @classmethod
    def _check_modo(cls, v: str) -> str:
        if v not in _MODOS:
            raise ValueError(f"modo must be one of {sorted(_MODOS)}")
        return v


class PresentacionEnviar(BaseModel):
    # The redactor who authorizes the final send (supplied by GEDOC), stored for
    # the audit trail. Optional — the send may be confirmed anonymously.
    confirmado_por: Optional[str] = None


# ---------------------------------------------------------------------------
# Response schemas.
# ---------------------------------------------------------------------------


class PresentacionCreated(BaseModel):
    id: int
    estado: str
    idempotency_key: str


class PresentacionStatus(BaseModel):
    id: int
    estado: str
    tipo_gestion: str
    modo: str
    rol_asignado: Optional[str] = None
    ruc: Optional[str] = None
    numero_identificador: Optional[str] = None
    fecha_envio: Optional[datetime] = None
    certificado_url: Optional[str] = None
    error_detail: Optional[str] = None
    confirmado_por: Optional[str] = None
    confirmado_at: Optional[datetime] = None


@router.post("", response_model=PresentacionCreated, status_code=status.HTTP_202_ACCEPTED)
def create_presentacion(
    body: PresentacionCreate,
    db: Session = Depends(get_db),
    key=Depends(require_presentacion_key),
) -> PresentacionCreated:
    """Queue a filing for presentation. Returns 202 with the created row.

    Idempotency: a repeat POST with the same ``idempotency_key`` returns the
    EXISTING presentación instead of creating a duplicate (FastAPI can't easily
    switch the status code per-branch, so the response stays 202 in both cases).
    Nothing is presented yet — the row sits at ``estado="en_cola"``.
    """
    existing = (
        db.query(Presentacion)
        .filter(Presentacion.idempotency_key == body.idempotency_key)
        .first()
    )
    if existing is not None:
        return PresentacionCreated(
            id=existing.id,
            estado=existing.estado,
            idempotency_key=existing.idempotency_key,
        )

    presentacion = Presentacion(
        idempotency_key=body.idempotency_key,
        api_key_id=key.id,
        tipo_gestion=body.tipo_gestion,
        modo=body.modo,
        competencia=body.competencia,
        corte=body.corte,
        tribunal=body.tribunal,
        rol=body.rol,
        anio=body.anio,
        procedimiento=body.procedimiento,
        materia=body.materia,
        dirigido_a_rut=body.dirigido_a_rut,
        credential_ref=body.credential_ref,
        payload={
            "litigantes": [lit.model_dump() for lit in body.litigantes],
            "documento_principal": body.documento_principal.model_dump(),
            "documentos": [doc.model_dump() for doc in body.documentos],
        },
        estado=PRES_EN_COLA,
    )
    db.add(presentacion)
    db.commit()
    db.refresh(presentacion)

    return PresentacionCreated(
        id=presentacion.id,
        estado=presentacion.estado,
        idempotency_key=presentacion.idempotency_key,
    )


@router.get("/{presentacion_id}", response_model=PresentacionStatus)
def get_presentacion(
    presentacion_id: int,
    db: Session = Depends(get_db),
    _key=Depends(require_presentacion_key),
) -> PresentacionStatus:
    """Return the current lifecycle state of a presentación. 404 if not found."""
    presentacion = (
        db.query(Presentacion).filter(Presentacion.id == presentacion_id).first()
    )
    if presentacion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Presentacion not found"
        )

    return PresentacionStatus(
        id=presentacion.id,
        estado=presentacion.estado,
        tipo_gestion=presentacion.tipo_gestion,
        modo=presentacion.modo,
        rol_asignado=presentacion.rol_asignado,
        ruc=presentacion.ruc,
        numero_identificador=presentacion.numero_identificador,
        fecha_envio=presentacion.fecha_envio,
        certificado_url=presentacion.certificado_url,
        error_detail=presentacion.error_detail,
        confirmado_por=presentacion.confirmado_por,
        confirmado_at=presentacion.confirmado_at,
    )


@router.post("/{presentacion_id}/enviar", response_model=PresentacionStatus)
def confirmar_envio(
    presentacion_id: int,
    body: PresentacionEnviar,
    db: Session = Depends(get_db),
    key=Depends(require_presentacion_key),
) -> PresentacionStatus:
    """Confirm the final send of a loaded filing (Opción 2 / semiauto).

    This is the redactor's authorization of the irreversible "Enviar Poder
    Judicial" step: it only flips the state from ``cargada_pendiente_envio`` to
    ``envio_confirmado`` and records who confirmed it. The actual OJV send is
    performed later by the station worker (a future slice) — nothing is sent
    here.

    State rules:
      * ``cargada_pendiente_envio`` -> ``envio_confirmado`` (stamps
        ``confirmado_por`` / ``confirmado_at``), returns 200.
      * ``envio_confirmado`` (already confirmed) -> idempotent no-op, returns
        the current state (200) without overwriting the audit fields.
      * any other estado -> 409 Conflict.
      * unknown id -> 404.
    """
    presentacion = (
        db.query(Presentacion).filter(Presentacion.id == presentacion_id).first()
    )
    if presentacion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Presentacion not found"
        )

    if presentacion.estado == PRES_CARGADA_PENDIENTE:
        now = datetime.utcnow()
        presentacion.estado = PRES_ENVIO_CONFIRMADO
        presentacion.confirmado_por = body.confirmado_por
        presentacion.confirmado_at = now
        presentacion.updated_at = now
        db.commit()
        db.refresh(presentacion)
    elif presentacion.estado != PRES_ENVIO_CONFIRMADO:
        # Idempotent for envio_confirmado; every other estado is a conflict.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No se puede confirmar el envío: la presentación está en estado "
                f"'{presentacion.estado}' (requiere 'cargada_pendiente_envio')."
            ),
        )

    return PresentacionStatus(
        id=presentacion.id,
        estado=presentacion.estado,
        tipo_gestion=presentacion.tipo_gestion,
        modo=presentacion.modo,
        rol_asignado=presentacion.rol_asignado,
        ruc=presentacion.ruc,
        numero_identificador=presentacion.numero_identificador,
        fecha_envio=presentacion.fecha_envio,
        certificado_url=presentacion.certificado_url,
        error_detail=presentacion.error_detail,
        confirmado_por=presentacion.confirmado_por,
        confirmado_at=presentacion.confirmado_at,
    )
