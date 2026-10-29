"""Stats endpoint — firm dashboard aggregates for the authenticated account."""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_current_lawyer,
    require_admin,
    require_auditor,
    _resolve_lawyer_id,
    resolve_case_scope,
    ALL_CASES,
)
from app.models.case import Case
from app.models.goal import Goal
from app.models.lawyer import Lawyer
from app.services.deadline_engine import medida_prejudicial_clause
from app.services.scraping_health import evaluar_salud_scraping, ultimo_bloqueo_ip
from app.services.lawyer_roster import (
    firm_dashboard_stats,
    firm_dashboard_stats_all,
    firm_risk_board,
    admin_dashboard_stats,
    lawyer_dashboard_stats,
)

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
    Counts EVERY civil case of the firm. It used to count only the cases owned by
    one account (``FIRM_LAWYER_RUT``); since the duplicate cleanup a case belongs
    to a single lawyer, so that account held about half of the portfolio and the
    marquee under-reported it. A medida prejudicial is neutral (no plazos) and
    stays out, like everywhere else.
    """
    counts = dict(
        db.query(Case.semaforo, func.count(Case.id))
        .filter(Case.competencia == "civil", ~medida_prejudicial_clause())
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
    """Return productivity stats scoped to the authenticated lawyer only.

    Computes ONLY this lawyer's cases (see ``lawyer_dashboard_stats``) instead
    of building the whole firm's breakdown and re-running the firm-wide litigante
    join twice — the old path did three firm-wide passes to return ~10 numbers.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")

    stats = lawyer_dashboard_stats(db, lawyer.rut)

    # Meta: Goal row with key "monthly_productivity"
    goal = db.query(Goal).filter(Goal.key == "monthly_productivity").first()
    meta = goal.value if goal else None

    return MyStatsResponse(
        rut=stats["rut"],
        nombre=stats["nombre"] or lawyer.name,
        case_count=stats["case_count"],
        rojo=stats["rojo"],
        amarillo=stats["amarillo"],
        verde=stats["verde"],
        otros=stats["otros"],
        stale=stats["stale"],
        actividad_mes=stats["actividad_mes"],
        proyeccion_mes=stats["proyeccion_mes"],
        meta=meta,
    )


class AdminSync(BaseModel):
    last_checked_at: str | None
    checked_1h: int = 0
    checked_24h: int
    movements_1h: int = 0
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
    plazo_fatal_incumplido: int = 0

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


class ScrapingBloqueoIp(BaseModel):
    desde: datetime
    detalle: str

    model_config = ConfigDict(from_attributes=True)


class ScrapingStatus(BaseModel):
    estado: str
    motivo: str
    ultimo_exito: datetime | None
    horas_sin_exito: float | None
    fallas_consecutivas: int
    bloqueo_ip: ScrapingBloqueoIp | None

    model_config = ConfigDict(from_attributes=True)


def _as_utc(value: datetime | None) -> datetime | None:
    """Stored timestamps are naive UTC; emit them as UTC instants (``...Z``)."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


# A plazo or semaforo is only as good as the last sync that confirmed it. Past this
# age the screens tell every user the data is unverified (product document R03).
DATA_STALE_HOURS = 24


class DataFreshness(BaseModel):
    """Age of the last successful sync, for every signed-in role. No cause, no IP detail."""
    verificado: bool
    umbral_horas: int
    horas_sin_exito: float | None
    ultimo_exito: datetime | None


@router.get("/data-freshness", response_model=DataFreshness)
async def get_data_freshness(
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """Is the PJUD data fresh enough to trust? Open to every signed-in role.

    Unlike ``/scraping-status`` (admin only, with the IP-block detail) this exposes
    only how old the last successful sync is. No sync on record is NOT verified:
    nothing has confirmed the data.
    """
    salud = evaluar_salud_scraping(db)
    horas = salud.horas_sin_exito
    return DataFreshness(
        verificado=horas is not None and horas <= DATA_STALE_HOURS,
        umbral_horas=DATA_STALE_HOURS,
        horas_sin_exito=round(horas, 1) if horas is not None else None,
        ultimo_exito=_as_utc(salud.ultimo_exito),
    )


@router.get("/scraping-status", response_model=ScrapingStatus)
async def get_scraping_status(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Scraping health plus the last IP-block event (admin banner). Read-only."""
    salud = evaluar_salud_scraping(db)
    bloqueo = ultimo_bloqueo_ip(db)
    return ScrapingStatus(
        estado=salud.estado,
        motivo=salud.motivo,
        ultimo_exito=_as_utc(salud.ultimo_exito),
        horas_sin_exito=(
            round(salud.horas_sin_exito, 1) if salud.horas_sin_exito is not None else None
        ),
        fallas_consecutivas=salud.fallas_consecutivas,
        bloqueo_ip=(
            ScrapingBloqueoIp(desde=_as_utc(bloqueo.enviada_at), detalle=bloqueo.detalle or "")
            if bloqueo is not None
            else None
        ),
    )
