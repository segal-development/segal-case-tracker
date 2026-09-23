"""Asignación por nivel — endpoints to reassign WHO internally works a causa.

Splits two concepts ``Case.lawyer_id`` used to conflate: provenance (whose
PJUD account the causa was scraped from — see
``app.services.sync_service.SyncService.sync_cases`` / ``existing_by_rol``,
which this module never touches) and business assignment (which internal
lawyer, by nivel, currently works the causa). The override lives on
``Case.assigned_lawyer_id`` (see ``app/models/case.py`` for
``effective_lawyer_id`` / ``EFFECTIVE_LAWYER_ID``).

Level rules the firm operates under (checked here against ``Lawyer.nivel``):
    M1 Baja -> Junior exclusivo
    M1 Alta -> Pleno o Senior
    M2      -> Pleno o Senior (un Junior nunca opera M2)
    M3      -> Senior exclusivo

Auth: read endpoints (``desajustes``, ``sugerencias``) allow admin or
auditor; mutating endpoints (``reasignar``, the DELETE override) are
admin-only.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, require_admin, require_auditor
from app.models.case import EFFECTIVE_LAWYER_ID, Case
from app.models.lawyer import Lawyer

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_REASIGNAR_BATCH = 500

# Nivel(es) que puede operar cada matriz. Ver docstring del módulo.
NIVELES_REQUERIDOS: Dict[str, List[str]] = {
    "M1 Baja": ["junior"],
    "M1 Alta": ["pleno", "senior"],
    "M2": ["pleno", "senior"],
    "M3": ["senior"],
}


# ============================================================================
# RESPONSE / REQUEST SCHEMAS
# ============================================================================


class LawyerActualOut(BaseModel):
    id: int
    nombre: str
    nivel: Optional[str] = None


class DesajusteItem(BaseModel):
    case_id: int
    rol: str
    caratulado: Optional[str] = None
    tribunal: Optional[str] = None
    matriz: str
    matriz_origen: Optional[str] = None
    lawyer_actual: LawyerActualOut
    niveles_requeridos: List[str]
    asignado: bool


class ResumenItem(BaseModel):
    matriz: str
    nivel_actual: Optional[str] = None
    causas: int


class DesajustesResponse(BaseModel):
    items: List[DesajusteItem]
    total: int
    page: int
    page_size: int
    resumen: List[ResumenItem]


class ReasignarRequest(BaseModel):
    case_ids: List[int] = Field(..., min_length=1, max_length=MAX_REASIGNAR_BATCH)
    lawyer_id: int
    motivo: Optional[str] = None


class OmitidaItem(BaseModel):
    case_id: int
    motivo: str


class ReasignarResponse(BaseModel):
    reasignadas: int
    omitidas: List[OmitidaItem]
    advertencias: List[str]


class SugerenciaLawyer(BaseModel):
    lawyer_id: int
    nombre: str
    nivel: str
    causas_actuales: int


class SugerenciasResponse(BaseModel):
    por_nivel: Dict[str, List[SugerenciaLawyer]]


# ============================================================================
# Helpers
# ============================================================================


def _mismatch_clause():
    """SQL clause: TRUE when ``Lawyer.nivel`` does not satisfy ``Case.matriz``.

    Built as an OR of per-matriz AND clauses so pagination/count stay exact
    at the DB level instead of loading every classified causa into Python.
    """
    from sqlalchemy import and_, or_

    clauses = [
        and_(
            Case.matriz == matriz,
            or_(Lawyer.nivel.is_(None), ~Lawyer.nivel.in_(niveles)),
        )
        for matriz, niveles in NIVELES_REQUERIDOS.items()
    ]
    return or_(*clauses)


def _caratulado(case: Case) -> Optional[str]:
    parts = [p for p in (case.plaintiff, case.defendant) if p]
    return "/".join(parts) if parts else None


# ============================================================================
# ENDPOINTS
# ============================================================================


@router.get("/desajustes", response_model=DesajustesResponse)
async def get_desajustes(
    matriz: Optional[str] = Query(
        None, description="Filtra por matriz: M1 Baja, M1 Alta, M2, M3"
    ),
    lawyer_id: Optional[int] = Query(
        None, description="Filtra por el abogado efectivo actual (assigned_lawyer_id o lawyer_id)"
    ),
    solo_firmes: bool = Query(
        True,
        description=(
            "Excluye causas con matriz_origen='sin_detalle' (clasificación "
            "provisional, no evidencia de un desajuste real)."
        ),
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Causas cuyo abogado efectivo tiene un nivel que no calza con su matriz."""
    query = (
        db.query(Case, Lawyer)
        .join(Lawyer, Lawyer.id == EFFECTIVE_LAWYER_ID)
        .options(joinedload(Case.court))
        .filter(Case.competencia == "civil")
        .filter(_mismatch_clause())
    )
    if solo_firmes:
        query = query.filter(Case.matriz_origen != "sin_detalle")
    if matriz:
        query = query.filter(Case.matriz == matriz)
    if lawyer_id is not None:
        query = query.filter(EFFECTIVE_LAWYER_ID == lawyer_id)

    total = query.count()

    rows = (
        query.order_by(Case.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [
        DesajusteItem(
            case_id=case.id,
            rol=case.rol,
            caratulado=_caratulado(case),
            tribunal=case.court.name if case.court else None,
            matriz=case.matriz,
            matriz_origen=case.matriz_origen,
            lawyer_actual=LawyerActualOut(id=lawyer.id, nombre=lawyer.name, nivel=lawyer.nivel),
            niveles_requeridos=NIVELES_REQUERIDOS.get(case.matriz, []),
            asignado=case.assigned_lawyer_id is not None,
        )
        for case, lawyer in rows
    ]

    resumen_rows = (
        query.with_entities(Case.matriz, Lawyer.nivel, func.count(Case.id))
        .group_by(Case.matriz, Lawyer.nivel)
        .all()
    )
    resumen = [
        ResumenItem(matriz=m, nivel_actual=n, causas=c) for m, n, c in resumen_rows
    ]

    return DesajustesResponse(
        items=items, total=total, page=page, page_size=page_size, resumen=resumen
    )


@router.post("/reasignar", response_model=ReasignarResponse)
async def reasignar(
    body: ReasignarRequest,
    admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Reasigna un lote de causas a otro abogado interno (admin only).

    NUNCA toca ``Case.lawyer_id`` (ver ``existing_by_rol`` en
    ``app.services.sync_service``); solo escribe el override
    ``assigned_lawyer_id``/``assigned_at``/``assigned_by_rut``/``assigned_motivo``.
    Si el abogado destino no tiene el nivel que exige la matriz de una
    causa, la reasignación NO se bloquea — el negocio puede decidir
    asignar fuera de nivel a propósito — pero se informa en ``advertencias``.
    """
    if len(body.case_ids) > MAX_REASIGNAR_BATCH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Máximo {MAX_REASIGNAR_BATCH} causas por llamada",
        )

    target = db.query(Lawyer).filter(Lawyer.id == body.lawyer_id).first()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Abogado destino no encontrado")
    if not target.is_firm_lawyer or not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El abogado destino debe ser un abogado activo de la firma",
        )

    requested_ids = list(dict.fromkeys(body.case_ids))  # dedupe, preserve order
    cases = db.query(Case).filter(Case.id.in_(requested_ids)).all()
    found_ids = {c.id for c in cases}
    missing_ids = [cid for cid in requested_ids if cid not in found_ids]

    omitidas = [
        OmitidaItem(case_id=cid, motivo="Causa no encontrada") for cid in missing_ids
    ]

    advertencias: List[str] = []
    now = datetime.utcnow()
    reasignadas = 0
    for case in cases:
        case.assigned_lawyer_id = target.id
        case.assigned_at = now
        case.assigned_by_rut = admin_rut
        case.assigned_motivo = body.motivo
        reasignadas += 1

        niveles = NIVELES_REQUERIDOS.get(case.matriz)
        if niveles and target.nivel not in niveles:
            advertencias.append(
                f"Causa {case.rol} (matriz {case.matriz}) requiere nivel "
                f"{'/'.join(niveles)}, pero {target.name} tiene nivel "
                f"{target.nivel or 'sin nivel'}."
            )

    db.commit()

    return ReasignarResponse(reasignadas=reasignadas, omitidas=omitidas, advertencias=advertencias)


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_asignacion(
    case_id: int,
    _admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Limpia el override de asignación (vuelve al abogado que sincroniza la causa)."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Causa no encontrada")

    case.assigned_lawyer_id = None
    case.assigned_at = None
    case.assigned_by_rut = None
    case.assigned_motivo = None
    db.commit()


@router.get("/sugerencias", response_model=SugerenciasResponse)
async def get_sugerencias(
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Abogados activos de la firma candidatos por nivel, con su carga actual.

    No arma una sugerencia por causa: entrega la lista de abogados activos
    por cada nivel requerido (junior/pleno/senior) junto a su cantidad
    actual de causas efectivas, para que el admin balancee la carga.
    """
    niveles = sorted({n for req in NIVELES_REQUERIDOS.values() for n in req})

    counts = dict(
        db.query(EFFECTIVE_LAWYER_ID, func.count(Case.id))
        .filter(Case.competencia == "civil")
        .group_by(EFFECTIVE_LAWYER_ID)
        .all()
    )

    lawyers = (
        db.query(Lawyer)
        .filter(
            Lawyer.is_firm_lawyer.is_(True),
            Lawyer.is_active.is_(True),
            Lawyer.nivel.in_(niveles),
        )
        .order_by(Lawyer.nivel, Lawyer.name)
        .all()
    )

    por_nivel: Dict[str, List[SugerenciaLawyer]] = {n: [] for n in niveles}
    for lw in lawyers:
        por_nivel[lw.nivel].append(
            SugerenciaLawyer(
                lawyer_id=lw.id,
                nombre=lw.name,
                nivel=lw.nivel,
                causas_actuales=counts.get(lw.id, 0),
            )
        )

    return SugerenciasResponse(por_nivel=por_nivel)
