"""Contract renewals (renovaciones) endpoints — new commercial module.

Minimal data entry (contract #, client RUT + name, handling lawyer, monthly
amount); everything else is derived: a renewal always runs 12 months, so
fecha_hasta = fecha_desde + 1 year, and total = monto_cuota * 12. The lawyer
selector is the firm's own active lawyers.
"""
import logging
from datetime import date, datetime
from typing import List, Optional

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db
from app.models.lawyer import Lawyer
from app.models.renovacion import Renovacion, CUOTAS_RENOVACION
from app.utils.rut import format_rut, normalize_rut

logger = logging.getLogger(__name__)
router = APIRouter()

MONTO_DEFAULT = 25_000


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class RenovacionCreate(BaseModel):
    numero_contrato: str
    cliente_rut: str
    cliente_nombre: str
    lawyer_id: int
    monto_cuota: int = MONTO_DEFAULT
    fecha_desde: Optional[date] = None  # defaults to today

    @field_validator("numero_contrato", "cliente_rut", "cliente_nombre")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("campo obligatorio")
        return v.strip()


class RenovacionResponse(BaseModel):
    id: int
    numero_contrato: str
    cliente_rut: str  # formatted for display
    cliente_nombre: str
    lawyer_id: int
    lawyer_nombre: Optional[str] = None
    fecha_desde: date
    fecha_hasta: date
    monto_cuota: int
    cuotas: int
    total: int
    created_by_name: Optional[str] = None


class AbogadoOption(BaseModel):
    lawyer_id: int
    nombre: str


class RenovacionResumen(BaseModel):
    count: int
    total_cuotas: int  # sum of monthly cuotas (monthly recaudación base)
    total_anual: int   # sum of totals (cuota × 12)


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


def _to_response(r: Renovacion) -> RenovacionResponse:
    return RenovacionResponse(
        id=r.id,
        numero_contrato=r.numero_contrato,
        cliente_rut=format_rut(r.cliente_rut) or r.cliente_rut,
        cliente_nombre=r.cliente_nombre,
        lawyer_id=r.lawyer_id,
        lawyer_nombre=r.lawyer.name if r.lawyer else None,
        fecha_desde=r.fecha_desde,
        fecha_hasta=r.fecha_hasta,
        monto_cuota=r.monto_cuota,
        cuotas=r.cuotas,
        total=r.total,
        created_by_name=r.created_by_name,
    )


def _period_bounds(periodo: Optional[str]) -> tuple[date, date]:
    if not periodo:
        now = datetime.utcnow()
        y, m = now.year, now.month
    else:
        try:
            y, m = (int(x) for x in periodo.split("-"))
            date(y, m, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
    return date(y, m, 1), date(y + (m == 12), (m % 12) + 1, 1)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/abogados", response_model=List[AbogadoOption])
async def list_abogados(
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """The firm's own active lawyers, for the renovación 'abogado' selector."""
    lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.is_firm_lawyer.is_(True), Lawyer.is_active.is_(True))
        .order_by(Lawyer.name)
        .all()
    )
    return [AbogadoOption(lawyer_id=l.id, nombre=l.name) for l in lawyers]


@router.post("", response_model=RenovacionResponse, status_code=status.HTTP_201_CREATED)
async def create_renovacion(
    body: RenovacionCreate,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Register a renewal. Derives fecha_hasta (+1 year) and total (cuota × 12)."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el usuario")
    if body.monto_cuota <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    abogado = db.query(Lawyer).filter(Lawyer.id == body.lawyer_id).first()
    if abogado is None or not abogado.is_firm_lawyer:
        raise HTTPException(status_code=404, detail="Abogado no encontrado en el estudio")

    desde = body.fecha_desde or date.today()
    hasta = desde + relativedelta(years=1)
    total = body.monto_cuota * CUOTAS_RENOVACION

    reno = Renovacion(
        numero_contrato=body.numero_contrato,
        cliente_rut=normalize_rut(body.cliente_rut),
        cliente_nombre=body.cliente_nombre,
        lawyer_id=abogado.id,
        fecha_desde=desde,
        fecha_hasta=hasta,
        monto_cuota=body.monto_cuota,
        cuotas=CUOTAS_RENOVACION,
        total=total,
        created_by_rut=actor.rut,
        created_by_name=actor.name,
    )
    db.add(reno)
    db.commit()
    db.refresh(reno)
    return _to_response(reno)


@router.get("", response_model=List[RenovacionResponse])
async def list_renovaciones(
    periodo: Optional[str] = Query(None, description="Filtrar por mes YYYY-MM (fecha de renovación)"),
    lawyer_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, description="Buscar por nombre, RUT o N° contrato"),
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """List renewals, most recent first, optionally filtered by month/lawyer/text."""
    query = db.query(Renovacion)
    if periodo:
        start, end = _period_bounds(periodo)
        query = query.filter(Renovacion.fecha_desde >= start, Renovacion.fecha_desde < end)
    if lawyer_id is not None:
        query = query.filter(Renovacion.lawyer_id == lawyer_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            Renovacion.cliente_nombre.ilike(like)
            | Renovacion.cliente_rut.ilike(like)
            | Renovacion.numero_contrato.ilike(like)
        )
    rows = query.order_by(Renovacion.fecha_desde.desc(), Renovacion.id.desc()).all()
    return [_to_response(r) for r in rows]


@router.get("/resumen", response_model=RenovacionResumen)
async def resumen_renovaciones(
    periodo: Optional[str] = Query(None, description="Mes YYYY-MM (default: mes actual)"),
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """Count + recaudación totals for a month."""
    start, end = _period_bounds(periodo)
    rows = (
        db.query(Renovacion)
        .filter(Renovacion.fecha_desde >= start, Renovacion.fecha_desde < end)
        .all()
    )
    return RenovacionResumen(
        count=len(rows),
        total_cuotas=sum(r.monto_cuota for r in rows),
        total_anual=sum(r.total for r in rows),
    )


@router.delete("/{renovacion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_renovacion(
    renovacion_id: int,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Delete a renewal (admin, or the person who registered it)."""
    actor = _resolve_lawyer(db, current_lawyer)
    reno = db.query(Renovacion).filter(Renovacion.id == renovacion_id).first()
    if reno is None:
        raise HTTPException(status_code=404, detail="Renovación no encontrada")
    is_admin = bool(actor and actor.role == "admin")
    if not is_admin and (actor is None or reno.created_by_rut != actor.rut):
        raise HTTPException(status_code=403, detail="Sin permiso para eliminar esta renovación")
    db.delete(reno)
    db.commit()
