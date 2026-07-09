"""Stats endpoint — firm dashboard aggregates for the authenticated account."""

import calendar
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_current_lawyer,
    require_auditor,
    _resolve_lawyer_id,
    resolve_case_scope,
    ALL_CASES,
)
from app.models.case import Case
from app.models.goal import Goal
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import (
    firm_dashboard_stats,
    firm_dashboard_stats_all,
    firm_risk_board,
    admin_dashboard_stats,
    case_ids_for_abogado,
)
from app.utils.rut import normalize_rut

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
    actividad_mes: int
    proyeccion_mes: int
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


class MyStatsResponse(BaseModel):
    rut: str
    nombre: str
    case_count: int
    rojo: int
    amarillo: int
    verde: int
    otros: int
    stale: int
    actividad_mes: int
    proyeccion_mes: int
    meta: int | None

    model_config = ConfigDict(from_attributes=True)


@router.get("/firm", response_model=FirmDashboardStats)
async def get_firm_stats(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return firm-wide dashboard stats: semaforo breakdown, stale cases, materias, and per-lawyer metrics.

    Auditor role: aggregates across EVERY study case (all lawyers), via
    ``firm_dashboard_stats_all``. Every other role stays scoped to its own
    account's cases via ``firm_dashboard_stats``.
    """
    scope = resolve_case_scope(db, current_lawyer)
    if scope is ALL_CASES:
        return firm_dashboard_stats_all(db)

    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    return firm_dashboard_stats(db, lawyer.rut)


class PublicOverview(BaseModel):
    """Aggregated firm semaforo counts for the (pre-auth) login screen — counts only."""
    rojo: int
    amarillo: int
    verde: int


@router.get("/public-overview", response_model=PublicOverview)
async def get_public_overview(db: Session = Depends(get_db)):
    """PUBLIC (no auth): firm semáforo counts for the login screen marquee.

    Exposes ONLY aggregated counts (no case data, no names, no personal info).
    The firm account is the one configured via FIRM_LAWYER_RUT (default the study
    account); falls back to the lawyer with the most civil cases.
    """
    import os
    from sqlalchemy import func

    firm_rut = os.environ.get("FIRM_LAWYER_RUT", "16021492-9")
    lawyer = db.query(Lawyer).filter(Lawyer.rut == firm_rut).first()
    if lawyer is None:
        row = (
            db.query(Case.lawyer_id, func.count(Case.id).label("c"))
            .filter(Case.competencia == "civil")
            .group_by(Case.lawyer_id)
            .order_by(func.count(Case.id).desc())
            .first()
        )
        lawyer = db.get(Lawyer, row.lawyer_id) if row else None
    if lawyer is None:
        return PublicOverview(rojo=0, amarillo=0, verde=0)

    counts = dict(
        db.query(Case.semaforo, func.count(Case.id))
        .filter(Case.lawyer_id == lawyer.id, Case.competencia == "civil")
        .group_by(Case.semaforo)
        .all()
    )
    return PublicOverview(
        rojo=counts.get("rojo", 0),
        amarillo=counts.get("amarillo", 0),
        verde=counts.get("verde", 0),
    )


@router.get("/me", response_model=MyStatsResponse)
async def get_my_stats(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return productivity stats scoped to the authenticated lawyer only."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")

    norm_rut = normalize_rut(lawyer.rut)

    # Get the full firm stats to find this lawyer's row
    stats = firm_dashboard_stats(db, lawyer.rut)
    row = next((r for r in stats["by_lawyer"] if r["rut"] == norm_rut), None)

    # Compute actividad_mes: cases in this lawyer's case set with last_movement_at in current month
    case_ids = case_ids_for_abogado(db, lawyer.rut, lawyer.rut)
    now = datetime.utcnow()
    actividad_mes = 0
    if case_ids:
        all_cases = (
            db.query(Case.id, Case.last_movement_at)
            .filter(Case.id.in_(case_ids))
            .all()
        )
        actividad_mes = sum(
            1 for c in all_cases
            if c.last_movement_at is not None
            and c.last_movement_at.year == now.year
            and c.last_movement_at.month == now.month
        )

    # Linear projection: round(actividad_mes / day_of_month * days_in_month)
    day = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    proyeccion_mes = round(actividad_mes / day * days_in_month) if day > 0 else actividad_mes

    # Meta: Goal row with key "monthly_productivity"
    goal = db.query(Goal).filter(Goal.key == "monthly_productivity").first()
    meta = goal.value if goal else None

    if row is None:
        # Lawyer has no civil case appearances — return zeros
        return MyStatsResponse(
            rut=norm_rut,
            nombre=lawyer.name,
            case_count=0,
            rojo=0,
            amarillo=0,
            verde=0,
            otros=0,
            stale=0,
            actividad_mes=0,
            proyeccion_mes=0,
            meta=meta,
        )

    return MyStatsResponse(
        rut=row["rut"],
        nombre=row["nombre"],
        case_count=row["case_count"],
        rojo=row["rojo"],
        amarillo=row["amarillo"],
        verde=row["verde"],
        otros=row["otros"],
        stale=row["stale"],
        actividad_mes=actividad_mes,
        proyeccion_mes=proyeccion_mes,
        meta=meta,
    )


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


class RiskBoardSemaforo(BaseModel):
    rojo: int
    amarillo: int
    verde: int
    gris: int

    model_config = ConfigDict(from_attributes=True)


class RiskBoardRiesgo(BaseModel):
    abandono_disponible: int
    prescripcion_cumplida: int
    en_apremio: int
    plazo_fatal_proximo: int
    plazo_fatal_vencido: int

    model_config = ConfigDict(from_attributes=True)


class RiskBoardLawyer(BaseModel):
    rut: str
    nombre: str
    rojo: int
    abandono_disponible: int
    en_apremio: int
    total: int

    model_config = ConfigDict(from_attributes=True)


class RiskBoardCriticalCase(BaseModel):
    case_id: int
    rol: str
    caratulado: str
    tribunal: str | None
    abogado_nombre: str
    semaforo: str | None
    next_deadline_at: date | None = None
    next_deadline_fatal: bool

    model_config = ConfigDict(from_attributes=True)


class RiskBoard(BaseModel):
    total: int
    semaforo: RiskBoardSemaforo
    riesgo: RiskBoardRiesgo
    by_lawyer: list[RiskBoardLawyer]
    top_critical: list[RiskBoardCriticalCase]

    model_config = ConfigDict(from_attributes=True)


@router.get("/risk-board", response_model=RiskBoard)
async def get_risk_board(
    db: Session = Depends(get_db),
    auditor_rut: str = Depends(require_auditor),
):
    """Firm-wide RISK BOARD (auditor/admin only): rolls up per-case exposure
    flags (semaforo, abandono_disponible, prescripcion_cumplida, en_apremio,
    fatal deadlines) into a single firm-wide view, with a per-abogado
    breakdown and the top ~15 most urgent cases.
    """
    return firm_risk_board(db, auditor_rut)
