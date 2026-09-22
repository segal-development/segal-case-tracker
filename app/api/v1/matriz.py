"""Matriz de clasificación endpoints (M1 Baja/M1 Alta/M2/M3).

Read endpoints follow the same visibility rule as ``GET /cases``: admin and
auditor see the firm-wide distribution, any other lawyer sees only their own
scope (``resolve_case_scope``/``apply_case_scope``). The mapping-editor
endpoints (``GET``/``PUT /matriz/mapeo``) are admin-only — this is the layer
the business tunes without a deploy (see ``app.services.matriz_classifier``).
"""
import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import (
    apply_case_scope,
    get_current_lawyer,
    get_db,
    require_admin,
    require_auditor,
    resolve_case_scope,
)
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo
from app.models.movement import Movement
from app.services.lawyer_roster import ALL_ABOGADO, _abogado_litigantes_by_case
from app.utils.rut import normalize_rut

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# RESPONSE / REQUEST SCHEMAS (inline, matching the rest of app/api/v1)
# ============================================================================


class MatrizCountItem(BaseModel):
    matriz: Optional[str] = None
    causas: int
    pct: float


class OrigenCountItem(BaseModel):
    origen: Optional[str] = None
    causas: int
    pct: float


class SinMapearItem(BaseModel):
    stage: Optional[str] = None
    causas: int


class MatrizDistribucionResponse(BaseModel):
    total: int
    por_matriz: List[MatrizCountItem]
    por_origen: List[OrigenCountItem]
    sin_mapear: List[SinMapearItem]
    computed_at: Optional[datetime] = None


class PorAbogadoItem(BaseModel):
    lawyer_id: int
    lawyer_name: str
    por_matriz: List[MatrizCountItem]
    total: int


class PorAbogadoResponse(BaseModel):
    items: List[PorAbogadoItem]


class MatrizMapeoResponse(BaseModel):
    pjud_stage: str
    matriz_etapa: str
    nota: Optional[str] = None
    activo: bool

    class Config:
        from_attributes = True


class MatrizMapeoUpdate(BaseModel):
    """Partial update — only provided fields are changed."""
    matriz_etapa: Optional[str] = None
    activo: Optional[bool] = None
    nota: Optional[str] = None


# ============================================================================
# Helpers
# ============================================================================


def _counted(counter: dict, total: int) -> list[dict]:
    items = [
        {"causas": causas, "pct": round((causas / total * 100), 2) if total else 0.0, "key": key}
        for key, causas in counter.items()
    ]
    items.sort(key=lambda i: (-i["causas"], str(i["key"] or "")))
    return items


def _sin_mapear(db: Session, case_ids: list[int]) -> List[SinMapearItem]:
    """Last-movement stage per unmapped causa, grouped and counted.

    ``matriz_origen == "no_mapeada"`` is derived at classification time but
    the triggering PJUD stage isn't a persisted Case column (only
    ``matriz``/``matriz_etapa``/``matriz_origen`` are) — so this re-derives
    it here, in ONE query over every candidate case's movements (never N+1).
    """
    if not case_ids:
        return []
    rows = (
        db.query(Movement.case_id, Movement.stage, Movement.movement_date, Movement.id)
        .filter(
            Movement.case_id.in_(case_ids),
            Movement.stage.isnot(None),
            Movement.stage != "",
        )
        .all()
    )
    latest: dict[int, tuple] = {}
    for case_id, stage, movement_date, mid in rows:
        key = (movement_date, mid)
        if case_id not in latest or key > latest[case_id][0]:
            latest[case_id] = (key, stage)

    counts = Counter(stage for _, stage in latest.values())
    return [
        SinMapearItem(stage=stage, causas=causas)
        for stage, causas in counts.most_common()
    ]


# ============================================================================
# ENDPOINTS
# ============================================================================


@router.get("/distribucion", response_model=MatrizDistribucionResponse)
async def get_distribucion(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Matriz distribution over the caller's scope (firm-wide for admin/auditor)."""
    scope = resolve_case_scope(db, current_lawyer)
    query = apply_case_scope(db.query(Case), scope)

    total = query.count()

    matriz_counts = dict(
        query.with_entities(Case.matriz, func.count()).group_by(Case.matriz).all()
    )
    origen_counts = dict(
        query.with_entities(Case.matriz_origen, func.count()).group_by(Case.matriz_origen).all()
    )

    por_matriz = [
        MatrizCountItem(matriz=item["key"], causas=item["causas"], pct=item["pct"])
        for item in _counted(matriz_counts, total)
    ]
    por_origen = [
        OrigenCountItem(origen=item["key"], causas=item["causas"], pct=item["pct"])
        for item in _counted(origen_counts, total)
    ]

    no_mapeada_ids = [
        cid
        for (cid,) in query.filter(Case.matriz_origen == "no_mapeada").with_entities(Case.id).all()
    ]
    sin_mapear = _sin_mapear(db, no_mapeada_ids)

    computed_at = query.with_entities(func.max(Case.matriz_computed_at)).scalar()

    return MatrizDistribucionResponse(
        total=total,
        por_matriz=por_matriz,
        por_origen=por_origen,
        sin_mapear=sin_mapear,
        computed_at=computed_at,
    )


@router.get("/por-abogado", response_model=PorAbogadoResponse)
async def get_por_abogado(
    _rut: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Matriz counts per firm lawyer, derived from abogado-of-record litigantes
    (same attribution model as ``resolve_case_scope`` — NOT ``Case.lawyer_id``,
    which is only the firm's bookkeeping owner under Approach C)."""
    by_case = _abogado_litigantes_by_case(db)

    # Sets (not lists) — a lawyer can appear as both AB.DDO and AP.DDO on the
    # same case, which must count that case once, not twice.
    case_ids_by_rut: dict[str, set[int]] = defaultdict(set)
    for case_id, litigantes in by_case.items():
        for lit in litigantes:
            if lit.participante in ALL_ABOGADO and lit.rut:
                case_ids_by_rut[normalize_rut(lit.rut)].add(case_id)

    all_case_ids = list(by_case.keys())
    matriz_by_case = dict(
        db.query(Case.id, Case.matriz).filter(Case.id.in_(all_case_ids)).all()
        if all_case_ids
        else []
    )

    firm_lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.is_firm_lawyer.is_(True))
        .order_by(Lawyer.name)
        .all()
    )

    items = []
    for lawyer in firm_lawyers:
        case_ids = list(case_ids_by_rut.get(normalize_rut(lawyer.rut), set()))
        counts = Counter(matriz_by_case.get(cid) for cid in case_ids)
        por_matriz = [
            MatrizCountItem(matriz=item["key"], causas=item["causas"], pct=item["pct"])
            for item in _counted(dict(counts), len(case_ids))
        ]
        items.append(
            PorAbogadoItem(
                lawyer_id=lawyer.id,
                lawyer_name=lawyer.name,
                por_matriz=por_matriz,
                total=len(case_ids),
            )
        )

    return PorAbogadoResponse(items=items)


@router.get("/mapeo", response_model=List[MatrizMapeoResponse])
async def list_mapeo(
    _rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """The full PJUD-stage -> matriz-etapa mapping table, for the business to review."""
    rows = db.query(MatrizPjudMapeo).order_by(MatrizPjudMapeo.pjud_stage).all()
    return [MatrizMapeoResponse.model_validate(r) for r in rows]


@router.put("/mapeo/{pjud_stage}", response_model=MatrizMapeoResponse)
async def update_mapeo(
    pjud_stage: str,
    payload: MatrizMapeoUpdate,
    _rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Edit one mapping row (matriz_etapa / activo / nota) without a deploy."""
    row = db.query(MatrizPjudMapeo).filter(MatrizPjudMapeo.pjud_stage == pjud_stage).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe un mapeo para la etapa PJUD {pjud_stage!r}",
        )

    if payload.matriz_etapa is not None:
        row.matriz_etapa = payload.matriz_etapa
    if payload.activo is not None:
        row.activo = payload.activo
    if payload.nota is not None:
        row.nota = payload.nota

    db.commit()
    db.refresh(row)
    return MatrizMapeoResponse.model_validate(row)
