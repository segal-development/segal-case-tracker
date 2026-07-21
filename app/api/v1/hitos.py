"""Hitos (milestone → bonus) endpoints — Slice 1.

Replaces the manual "SISTEMA DE HITOS" sheet: a pre-loaded hito-type catalog, a
guided entry with a MANDATORY PJUD evidence capture, admin approval, and a
per-lawyer monthly total. Firm rule enforced here: a hito can never be approved
without evidence ("sin evidencia no se paga").
"""
import hashlib
import io
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, require_admin
from app.models.hito import Hito, HitoTipo, HITO_APROBADO, HITO_PENDIENTE, HITO_RECHAZADO
from app.models.lawyer import Lawyer
from app.services import bono_cierre_service as cierre_svc

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_EVIDENCE_BYTES = 15 * 1024 * 1024  # 15 MB
_ALLOWED_EVIDENCE = {"image/png", "image/jpeg", "image/webp", "application/pdf"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class HitoTipoResponse(BaseModel):
    id: int
    code: str
    label: str
    nivel: str
    valor_bruto: int
    etapa_tramite: Optional[str] = None
    verificacion: Optional[str] = None

    class Config:
        from_attributes = True


class HitoResponse(BaseModel):
    id: int
    lawyer_id: int
    lawyer_nombre: Optional[str] = None
    tipo_label: str
    nivel: str
    valor_bruto: int
    fecha_hito: date
    rol_causa: Optional[str] = None
    procedimiento: Optional[str] = None
    descripcion: Optional[str] = None
    etapa_sysgal: Optional[str] = None
    tramite_sysgal: Optional[str] = None
    tiene_evidencia: bool
    estado: str
    created_by_name: Optional[str] = None
    aprobado_by_name: Optional[str] = None
    aprobado_at: Optional[datetime] = None
    rechazo_motivo: Optional[str] = None


class HitoResumenRow(BaseModel):
    lawyer_id: int
    lawyer_nombre: str
    aprobados: int
    total_bruto: int
    pendientes: int


class RechazoBody(BaseModel):
    motivo: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_lawyer(db: Session, current_lawyer: dict) -> Optional[Lawyer]:
    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if sub is None:
        return None
    if isinstance(sub, int) or (isinstance(sub, str) and str(sub).isdigit()):
        return db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    return db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()


def _is_admin(lawyer: Optional[Lawyer]) -> bool:
    return bool(lawyer and lawyer.role == "admin")


def _to_response(h: Hito) -> HitoResponse:
    return HitoResponse(
        id=h.id,
        lawyer_id=h.lawyer_id,
        lawyer_nombre=h.lawyer.name if h.lawyer else None,
        tipo_label=h.tipo.label if h.tipo else "",
        nivel=h.tipo.nivel if h.tipo else "",
        valor_bruto=h.valor_bruto,
        fecha_hito=h.fecha_hito,
        rol_causa=h.rol_causa,
        procedimiento=h.procedimiento,
        descripcion=h.descripcion,
        etapa_sysgal=h.etapa_sysgal,
        tramite_sysgal=h.tramite_sysgal,
        tiene_evidencia=h.tiene_evidencia,
        estado=h.estado,
        created_by_name=h.created_by_name,
        aprobado_by_name=h.aprobado_by_name,
        aprobado_at=h.aprobado_at,
        rechazo_motivo=h.rechazo_motivo,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/tipos", response_model=List[HitoTipoResponse])
async def list_hito_tipos(
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """The hito-type catalog (for the entry form: label → value auto-fills)."""
    return (
        db.query(HitoTipo)
        .filter(HitoTipo.activo.is_(True))
        .order_by(HitoTipo.orden)
        .all()
    )


@router.post("", response_model=HitoResponse, status_code=status.HTTP_201_CREATED)
async def create_hito(
    hito_tipo_id: int = Form(...),
    fecha_hito: date = Form(...),
    rol_causa: Optional[str] = Form(None),
    procedimiento: Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    etapa_sysgal: Optional[str] = Form(None),
    tramite_sysgal: Optional[str] = Form(None),
    lawyer_id: Optional[int] = Form(None),  # admins may register for another lawyer
    evidencia: Optional[UploadFile] = File(None),  # PJUD capture — optional
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Register a hito. PJUD evidence is optional."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")

    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")

    tipo = db.query(HitoTipo).filter(HitoTipo.id == hito_tipo_id, HitoTipo.activo.is_(True)).first()
    if tipo is None:
        raise HTTPException(status_code=404, detail="Tipo de hito no encontrado")

    # A lawyer registers hitos for themselves; only an admin may set another lawyer.
    target_lawyer_id = actor.id
    if lawyer_id is not None and lawyer_id != actor.id:
        if not _is_admin(actor):
            raise HTTPException(status_code=403, detail="Solo un admin puede registrar hitos de otro abogado")
        if db.query(Lawyer).filter(Lawyer.id == lawyer_id).first() is None:
            raise HTTPException(status_code=404, detail="Abogado no encontrado")
        target_lawyer_id = lawyer_id

    # Evidence is optional. If provided, validate + store it.
    storage_uri = ev_filename = ev_content_type = None
    data = await evidencia.read() if evidencia is not None else b""
    if data:
        if len(data) > _MAX_EVIDENCE_BYTES:
            raise HTTPException(status_code=413, detail="La evidencia supera el tamaño máximo (15 MB)")
        content_type = evidencia.content_type or "application/octet-stream"
        if content_type not in _ALLOWED_EVIDENCE:
            raise HTTPException(
                status_code=415,
                detail="Formato de evidencia no permitido (usa PNG, JPG, WEBP o PDF)",
            )
        from app.config import settings
        from app.services.storage_service import get_storage_backend

        digest = hashlib.sha256(data).hexdigest()[:16]
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}.get(content_type, "bin")
        key = f"hitos/evidencia/{target_lawyer_id}/{digest}.{ext}"
        storage_uri = get_storage_backend(settings).upload(data, key, content_type=content_type)
        ev_filename = evidencia.filename
        ev_content_type = content_type

    hito = Hito(
        lawyer_id=target_lawyer_id,
        hito_tipo_id=tipo.id,
        valor_bruto=tipo.valor_bruto,  # snapshot
        fecha_hito=fecha_hito,
        rol_causa=rol_causa,
        procedimiento=procedimiento,
        descripcion=descripcion,
        etapa_sysgal=etapa_sysgal or tipo.etapa_tramite,
        tramite_sysgal=tramite_sysgal,
        evidencia_storage_key=storage_uri,
        evidencia_filename=ev_filename,
        evidencia_content_type=ev_content_type,
        estado=HITO_PENDIENTE,
        created_by_rut=actor.rut,
        created_by_name=actor.name,
    )
    db.add(hito)
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.get("", response_model=List[HitoResponse])
async def list_hitos(
    periodo: Optional[str] = Query(None, description="Filtrar por mes YYYY-MM"),
    lawyer_id: Optional[int] = Query(None),
    estado: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """List hitos. Admins see all; a lawyer sees only their own."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")

    q = db.query(Hito)
    if not _is_admin(actor):
        q = q.filter(Hito.lawyer_id == actor.id)  # non-admins: own hitos only
    elif lawyer_id is not None:
        q = q.filter(Hito.lawyer_id == lawyer_id)
    if estado:
        q = q.filter(Hito.estado == estado)
    if periodo:
        try:
            y, m = (int(x) for x in periodo.split("-"))
            start = date(y, m, 1)
            end = date(y + (m == 12), (m % 12) + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
        q = q.filter(Hito.fecha_hito >= start, Hito.fecha_hito < end)

    return [_to_response(h) for h in q.order_by(Hito.fecha_hito.desc(), Hito.id.desc()).all()]


@router.post("/{hito_id}/aprobar", response_model=HitoResponse)
async def aprobar_hito(
    hito_id: int,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Approve a hito (admin only)."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    hito.estado = HITO_APROBADO
    hito.aprobado_by_rut = admin_rut
    hito.aprobado_by_name = admin.name if admin else None
    hito.aprobado_at = datetime.utcnow()
    hito.rechazo_motivo = None
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.post("/{hito_id}/rechazar", response_model=HitoResponse)
async def rechazar_hito(
    hito_id: int,
    body: RechazoBody,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Reject a hito (admin only), with an optional reason."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    hito.estado = HITO_RECHAZADO
    hito.aprobado_by_rut = admin_rut
    hito.aprobado_by_name = admin.name if admin else None
    hito.aprobado_at = datetime.utcnow()
    hito.rechazo_motivo = body.motivo
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.get("/resumen", response_model=List[HitoResumenRow])
async def resumen_hitos(
    periodo: Optional[str] = Query(None, description="Mes YYYY-MM (default: mes actual)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Per-lawyer totals for a period: approved count + gross sum + pending count."""
    if periodo:
        try:
            y, m = (int(x) for x in periodo.split("-"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
    else:
        now = datetime.utcnow()
        y, m = now.year, now.month
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)

    rows = (
        db.query(Hito)
        .filter(Hito.fecha_hito >= start, Hito.fecha_hito < end)
        .all()
    )
    by_lawyer: dict[int, dict] = {}
    for h in rows:
        r = by_lawyer.setdefault(
            h.lawyer_id,
            {"lawyer_id": h.lawyer_id, "lawyer_nombre": h.lawyer.name if h.lawyer else "",
             "aprobados": 0, "total_bruto": 0, "pendientes": 0},
        )
        if h.estado == HITO_APROBADO:
            r["aprobados"] += 1
            r["total_bruto"] += h.valor_bruto
        elif h.estado == HITO_PENDIENTE:
            r["pendientes"] += 1
    result = sorted(by_lawyer.values(), key=lambda x: x["total_bruto"], reverse=True)
    return [HitoResumenRow(**r) for r in result]


@router.get("/{hito_id}/evidencia")
async def get_evidencia(
    hito_id: int,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Stream a hito's PJUD evidence. Admins, or the owning lawyer, only."""
    actor = _resolve_lawyer(db, current_lawyer)
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if not _is_admin(actor) and (actor is None or actor.id != hito.lawyer_id):
        raise HTTPException(status_code=403, detail="Sin acceso a esta evidencia")
    if not hito.evidencia_storage_key:
        raise HTTPException(status_code=404, detail="Sin evidencia")

    from app.config import settings
    from app.services.storage_service import get_storage_backend

    data = get_storage_backend(settings).retrieve(hito.evidencia_storage_key)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=hito.evidencia_content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{hito.evidencia_filename or "evidencia"}"'},
    )
