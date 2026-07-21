"""Bonus variables (V1–V4) + monthly liquidación — Hitos Slice 2.

Dirección Jurídica enters the manual monthly inputs (client/case counts,
complaints, renewals) per lawyer; the system computes V1–V4 and assembles the
liquidación (Fijo + Hitos aprobados + V1 + V3_neta + V2). All endpoints are
admin-only — payroll data. The math lives in ``app.services.bono_calc``.
"""
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.bono import BonoVariables
from app.models.hito import Hito, HITO_APROBADO
from app.models.lawyer import Lawyer
from app.services import bono_calc

logger = logging.getLogger(__name__)
router = APIRouter()

_NIVELES = {bono_calc.JUNIOR, bono_calc.PLENO}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class BonoParametros(BaseModel):
    fijo_junior: int
    fijo_pleno: int
    v2_por_renovacion: int
    v4_pesos: dict  # {"leve":0.05,"medio":0.15,"grave":0.30}


class BonoVariablesIn(BaseModel):
    clientes_m2: int = 0
    clientes_activos: int = 0
    causas_asignadas: int = 0
    causas_cumplidas: int = 0
    reclamos_leve: int = 0
    reclamos_medio: int = 0
    reclamos_grave: int = 0
    renovaciones: int = 0
    verificado_dj: bool = False


class BonoRow(BaseModel):
    lawyer_id: int
    lawyer_nombre: str
    nivel: str
    has_row: bool  # whether variables were entered for this period
    verificado_dj: bool
    # manual inputs
    clientes_m2: int
    clientes_activos: int
    causas_asignadas: int
    causas_cumplidas: int
    reclamos_leve: int
    reclamos_medio: int
    reclamos_grave: int
    renovaciones: int
    # computed breakdown
    fijo: int
    v1_pct_activacion: float
    v1_valor_cliente: int
    v1_bruto: int
    v3_pct_cumplimiento: float
    v3_tramo_bruto: int
    v4_pct: float
    v3_neta: int
    v2_bruto: int
    hitos_aprobados: int
    total_bono_gestion: int
    total_bruto: int


class BonoTotales(BaseModel):
    fijo: int
    hitos_aprobados: int
    v1_bruto: int
    v3_neta: int
    v2_bruto: int
    total_bono_gestion: int
    total_bruto: int


class LiquidacionResponse(BaseModel):
    periodo: str
    rows: List[BonoRow]
    totales: BonoTotales


class NivelIn(BaseModel):
    nivel: Optional[str] = None  # "junior" | "pleno" | null (removes from bonus)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _period_bounds(periodo: Optional[str]) -> tuple[str, date, date]:
    if periodo:
        try:
            y, m = (int(x) for x in periodo.split("-"))
            date(y, m, 1)  # validate
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
    else:
        now = datetime.utcnow()
        y, m = now.year, now.month
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return f"{y:04d}-{m:02d}", start, end


def _hitos_aprobados_map(db: Session, start: date, end: date) -> dict[int, int]:
    """Sum of approved hito gross value per lawyer for the period."""
    rows = (
        db.query(Hito)
        .filter(
            Hito.fecha_hito >= start,
            Hito.fecha_hito < end,
            Hito.estado == HITO_APROBADO,
        )
        .all()
    )
    out: dict[int, int] = {}
    for h in rows:
        out[h.lawyer_id] = out.get(h.lawyer_id, 0) + h.valor_bruto
    return out


def _build_row(lawyer: Lawyer, var: Optional[BonoVariables], hitos_aprobados: int) -> BonoRow:
    nivel = (var.nivel if var else None) or lawyer.nivel or bono_calc.JUNIOR
    inputs = dict(
        clientes_m2=var.clientes_m2 if var else 0,
        clientes_activos=var.clientes_activos if var else 0,
        causas_asignadas=var.causas_asignadas if var else 0,
        causas_cumplidas=var.causas_cumplidas if var else 0,
        reclamos_leve=var.reclamos_leve if var else 0,
        reclamos_medio=var.reclamos_medio if var else 0,
        reclamos_grave=var.reclamos_grave if var else 0,
        renovaciones=var.renovaciones if var else 0,
    )
    b = bono_calc.compute(nivel, hitos_aprobados=hitos_aprobados, **inputs)
    return BonoRow(
        lawyer_id=lawyer.id,
        lawyer_nombre=lawyer.name,
        nivel=nivel,
        has_row=var is not None,
        verificado_dj=bool(var.verificado_dj) if var else False,
        **inputs,
        fijo=b["fijo"],
        v1_pct_activacion=b["v1_pct_activacion"],
        v1_valor_cliente=b["v1_valor_cliente"],
        v1_bruto=b["v1_bruto"],
        v3_pct_cumplimiento=b["v3_pct_cumplimiento"],
        v3_tramo_bruto=b["v3_tramo_bruto"],
        v4_pct=b["v4_pct"],
        v3_neta=b["v3_neta"],
        v2_bruto=b["v2_bruto"],
        hitos_aprobados=b["hitos_aprobados"],
        total_bono_gestion=b["total_bono_gestion"],
        total_bruto=b["total_bruto"],
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/parametros", response_model=BonoParametros)
async def get_parametros(_admin_rut: str = Depends(require_admin)):
    """The fixed bonus parameters, for display in the UI."""
    return BonoParametros(
        fijo_junior=bono_calc.FIJO[bono_calc.JUNIOR],
        fijo_pleno=bono_calc.FIJO[bono_calc.PLENO],
        v2_por_renovacion=bono_calc.V2_POR_RENOVACION,
        v4_pesos={
            "leve": bono_calc.V4_PESO_LEVE,
            "medio": bono_calc.V4_PESO_MEDIO,
            "grave": bono_calc.V4_PESO_GRAVE,
        },
    )


@router.get("/liquidacion", response_model=LiquidacionResponse)
async def get_liquidacion(
    periodo: Optional[str] = Query(None, description="Mes YYYY-MM (default: mes actual)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Full monthly liquidación for every bonus lawyer (nivel set), with totals."""
    periodo, start, end = _period_bounds(periodo)
    lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.nivel.in_(list(_NIVELES)))
        .order_by(Lawyer.nivel, Lawyer.name)
        .all()
    )
    hitos_map = _hitos_aprobados_map(db, start, end)
    vars_map = {
        v.lawyer_id: v
        for v in db.query(BonoVariables).filter(BonoVariables.periodo == periodo).all()
    }

    rows = [_build_row(lw, vars_map.get(lw.id), hitos_map.get(lw.id, 0)) for lw in lawyers]
    totales = BonoTotales(
        fijo=sum(r.fijo for r in rows),
        hitos_aprobados=sum(r.hitos_aprobados for r in rows),
        v1_bruto=sum(r.v1_bruto for r in rows),
        v3_neta=sum(r.v3_neta for r in rows),
        v2_bruto=sum(r.v2_bruto for r in rows),
        total_bono_gestion=sum(r.total_bono_gestion for r in rows),
        total_bruto=sum(r.total_bruto for r in rows),
    )
    return LiquidacionResponse(periodo=periodo, rows=rows, totales=totales)


@router.put("/variables/{lawyer_id}", response_model=BonoRow)
async def upsert_variables(
    lawyer_id: int,
    body: BonoVariablesIn,
    periodo: str = Query(..., description="Mes YYYY-MM"),
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Enter/update the manual bonus inputs for a lawyer/period (Dirección Jurídica)."""
    periodo, _start, _end = _period_bounds(periodo)
    lawyer = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")
    if lawyer.nivel not in _NIVELES:
        raise HTTPException(
            status_code=409,
            detail="El abogado no tiene nivel (junior/pleno) asignado para el bono",
        )
    for field in ("clientes_m2", "clientes_activos", "causas_asignadas", "causas_cumplidas",
                  "reclamos_leve", "reclamos_medio", "reclamos_grave", "renovaciones"):
        if getattr(body, field) < 0:
            raise HTTPException(status_code=400, detail=f"{field} no puede ser negativo")
    if body.clientes_activos > body.clientes_m2 and body.clientes_m2 > 0:
        raise HTTPException(status_code=400, detail="Clientes activos no puede superar a los M-2")
    if body.causas_cumplidas > body.causas_asignadas and body.causas_asignadas > 0:
        raise HTTPException(status_code=400, detail="Causas cumplidas no puede superar a las asignadas")

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    var = (
        db.query(BonoVariables)
        .filter(BonoVariables.lawyer_id == lawyer_id, BonoVariables.periodo == periodo)
        .first()
    )
    if var is None:
        var = BonoVariables(
            lawyer_id=lawyer_id,
            periodo=periodo,
            nivel=lawyer.nivel,
            created_by_rut=admin_rut,
        )
        db.add(var)
    var.nivel = lawyer.nivel  # re-snapshot on each edit
    var.clientes_m2 = body.clientes_m2
    var.clientes_activos = body.clientes_activos
    var.causas_asignadas = body.causas_asignadas
    var.causas_cumplidas = body.causas_cumplidas
    var.reclamos_leve = body.reclamos_leve
    var.reclamos_medio = body.reclamos_medio
    var.reclamos_grave = body.reclamos_grave
    var.renovaciones = body.renovaciones
    var.verificado_dj = body.verificado_dj
    var.updated_by_rut = admin_rut
    var.updated_by_name = admin.name if admin else None
    db.commit()
    db.refresh(var)

    _p, start, end = _period_bounds(periodo)
    hitos = _hitos_aprobados_map(db, start, end).get(lawyer_id, 0)
    return _build_row(lawyer, var, hitos)


@router.put("/nivel/{lawyer_id}", response_model=dict)
async def set_nivel(
    lawyer_id: int,
    body: NivelIn,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Assign/clear a lawyer's bonus tier (junior/pleno/null)."""
    if body.nivel is not None and body.nivel not in _NIVELES:
        raise HTTPException(status_code=400, detail="nivel debe ser 'junior', 'pleno' o null")
    lawyer = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")
    lawyer.nivel = body.nivel
    db.commit()
    return {"lawyer_id": lawyer_id, "nivel": lawyer.nivel}
