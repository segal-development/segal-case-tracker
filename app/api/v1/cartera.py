"""Cartera snapshot endpoints — the frozen monthly portfolio for tramitación KPIs.

"Cartera del mes = snapshot del día 1 a las 00:00 hrs": every tramitación KPI
is computed against a snapshot taken by ``app.services.cartera_snapshot``,
never against the live ``cases`` table directly. Read endpoints follow the
same admin/auditor visibility as the rest of the transversal views
(``require_auditor`` allows both); taking a new snapshot (``POST
/cartera/snapshot``) is admin-only, mirroring ``app.api.v1.asignacion``.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin, require_auditor
from app.models.cartera_snapshot import CarteraSnapshotRun
from app.services.cartera_snapshot import (
    comparar_periodos,
    frescura_actual,
    periodo_actual,
    snapshot_detalle,
    tomar_snapshot,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# RESPONSE / REQUEST SCHEMAS
# ============================================================================


class FrescuraBuckets(BaseModel):
    nunca: int
    ultimos_7d: int
    entre_7_30d: int
    entre_30_90d: int
    mas_90d: int


class FrescuraBlock(BaseModel):
    total: int
    buckets: FrescuraBuckets
    pct_al_dia: float
    advertencia: Optional[str] = None


class FrescuraPorAbogadoItem(FrescuraBlock):
    lawyer_id: int
    nombre: str


class FrescuraResponse(FrescuraBlock):
    por_abogado: List[FrescuraPorAbogadoItem]


class SnapshotRunItem(BaseModel):
    periodo: str
    tomado_at: datetime
    causas: int
    tomado_por: Optional[str] = None


class SnapshotsListResponse(BaseModel):
    items: List[SnapshotRunItem]


class MatrizCountItem(BaseModel):
    matriz: Optional[str] = None
    causas: int
    pct: float


class PorAbogadoDetalleItem(BaseModel):
    lawyer_id: int
    nombre: str
    nivel: Optional[str] = None
    causas: int
    por_matriz: Dict[str, int]


class SnapshotDetalleResponse(BaseModel):
    periodo: str
    tomado_at: datetime
    causas: int
    por_matriz: List[MatrizCountItem]
    por_abogado: List[PorAbogadoDetalleItem]
    frescura: FrescuraBlock


class ComparacionMatrizItem(BaseModel):
    matriz: Optional[str] = None
    desde: int
    hasta: int
    delta: int


class ComparacionAbogadoItem(BaseModel):
    lawyer_id: int
    nombre: str
    desde: int
    hasta: int
    delta: int


class ComparacionResponse(BaseModel):
    desde: str
    hasta: str
    por_matriz: List[ComparacionMatrizItem]
    por_abogado: List[ComparacionAbogadoItem]


class TomarSnapshotRequest(BaseModel):
    periodo: Optional[str] = None
    reemplazar: bool = False


class TomarSnapshotResponse(BaseModel):
    periodo: str
    # Explicit label so this is never misread as "el snapshot del día 1" —
    # it is the moment THIS call ran, which may be any day of the month
    # (manual retake) or the automatic first-cycle-of-the-month take.
    tomado_at: datetime
    causas: int
    tomado_por: Optional[str] = None
    por_matriz: Dict[str, int]
    por_nivel: Dict[str, int]
    frescura: FrescuraBlock


# ============================================================================
# ENDPOINTS
# ============================================================================


@router.get("/snapshots", response_model=SnapshotsListResponse)
async def list_snapshots(
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """List every period a cartera snapshot has been taken for, newest first."""
    runs = (
        db.query(CarteraSnapshotRun).order_by(CarteraSnapshotRun.periodo.desc()).all()
    )
    return SnapshotsListResponse(
        items=[
            SnapshotRunItem(
                periodo=r.periodo, tomado_at=r.tomado_at, causas=r.causas, tomado_por=r.tomado_por
            )
            for r in runs
        ]
    )


@router.get("/snapshot/{periodo}", response_model=SnapshotDetalleResponse)
async def get_snapshot(
    periodo: str,
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Full per-matriz + per-abogado breakdown for an already-taken period."""
    detalle = snapshot_detalle(db, periodo)
    if detalle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe un snapshot de cartera para el periodo {periodo!r}",
        )
    return SnapshotDetalleResponse(**detalle)


@router.get("/comparar", response_model=ComparacionResponse)
async def comparar(
    desde: str = Query(..., description="Periodo de origen, formato YYYY-MM"),
    hasta: str = Query(..., description="Periodo de destino, formato YYYY-MM"),
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Per-matriz and per-abogado deltas between two snapshot periods."""
    return ComparacionResponse(**comparar_periodos(db, desde, hasta))


@router.post("/snapshot", response_model=TomarSnapshotResponse)
async def post_snapshot(
    payload: TomarSnapshotRequest,
    rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Take (or, without ``reemplazar``, return the existing) snapshot for a period.

    Defaults to the current period when ``periodo`` is omitted. The response
    always carries the exact ``tomado_at`` moment this call resolved to, so
    it is never misread as "the day-1 snapshot" when taken mid-month.
    """
    periodo = payload.periodo or periodo_actual()
    summary = tomar_snapshot(db, periodo, tomado_por=rut, reemplazar=payload.reemplazar)
    return TomarSnapshotResponse(**summary)


@router.get("/frescura", response_model=FrescuraResponse)
async def get_frescura(
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Live freshness of the current civil portfolio (not a snapshot read).

    Every activity KPI should be shown alongside this so the reader knows
    how stale the underlying PJUD data is.
    """
    return FrescuraResponse(**frescura_actual(db))
