"""Stats endpoint — firm dashboard aggregates for the authenticated account."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer, _resolve_lawyer_id
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_dashboard_stats, admin_dashboard_stats

router = APIRouter()


class SemaforoBreakdown(BaseModel):
    rojo: int
    amarillo: int
    verde: int
    otros: int

    model_config = ConfigDict(from_attributes=True)


class MateriaCount(BaseModel):
    materia: str
    count: int

    model_config = ConfigDict(from_attributes=True)


class StageCount(BaseModel):
    stage: str
    count: int

    model_config = ConfigDict(from_attributes=True)


class FirmTotals(BaseModel):
    cases: int
    semaforo: SemaforoBreakdown
    stale: int
    by_materia: list[MateriaCount]
    by_procedural_state: list[StageCount]

    model_config = ConfigDict(from_attributes=True)


class LawyerStats(BaseModel):
    rut: str
    nombre: str
    case_count: int
    rojo: int
    amarillo: int
    verde: int
    otros: int
    stale: int

    model_config = ConfigDict(from_attributes=True)


class FirmDashboardStats(BaseModel):
    totals: FirmTotals
    by_lawyer: list[LawyerStats]

    model_config = ConfigDict(from_attributes=True)


@router.get("/firm", response_model=FirmDashboardStats)
async def get_firm_stats(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return firm-wide dashboard stats: semaforo breakdown, stale cases, materias, and per-lawyer metrics."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    return firm_dashboard_stats(db, lawyer.rut)


class AdminSync(BaseModel):
    last_checked_at: str | None
    checked_24h: int
    pending_detail: int
    stale_30d: int

    model_config = ConfigDict(from_attributes=True)


class AdminDocuments(BaseModel):
    stored: int
    pending: int
    failed: int
    unavailable: int

    model_config = ConfigDict(from_attributes=True)


class AdminQuality(BaseModel):
    total_cases: int
    with_semaforo: int
    with_movements: int
    with_litigantes: int
    sin_asignar: int

    model_config = ConfigDict(from_attributes=True)


class AdminDashboardStats(BaseModel):
    sync: AdminSync
    documents: AdminDocuments
    quality: AdminQuality

    model_config = ConfigDict(from_attributes=True)


@router.get("/admin", response_model=AdminDashboardStats)
async def get_admin_stats(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return Admin dashboard stats: sync freshness, document pipeline, data quality."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    return admin_dashboard_stats(db, lawyer.rut)
