"""Sysgal cobertura admin endpoints — manual sync trigger + cache status.

Not to be confused with ``app/api/sysgal`` (the API Sysgal calls INTO us);
this router is our side of the outbound coverage lookup.
"""

from datetime import date, datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin, require_auditor
from app.config import settings
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.services.sysgal_cobertura import COBERTURAS, derive_cobertura
from app.services.sysgal_sync import sync_sysgal_estados

router = APIRouter()


class SysgalSyncResponse(BaseModel):
    skipped: bool
    consultados: int
    encontrados: int
    no_encontrados: int
    errores: int
    chunks: int


class SysgalStatusResponse(BaseModel):
    configured: bool
    last_synced_at: Optional[datetime] = None
    total_ruts: int
    por_cobertura: Dict[str, int]


@router.post("/sync", response_model=SysgalSyncResponse)
async def run_sysgal_sync(
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Run the Sysgal cache refresh now (same job the worker runs each cycle)."""
    return sync_sysgal_estados(db)


@router.get("/status", response_model=SysgalStatusResponse)
async def sysgal_status(
    _auditor: str = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    """Cache health: configured flag, last sync time and cobertura breakdown."""
    configured = bool(settings.SYSGAL_BASE_URL and settings.SYSGAL_API_KEY)
    last_synced_at = db.query(func.max(ClienteSysgalEstado.synced_at)).scalar()

    # The cache is small (~1.4k rows); derive in Python to reuse the one rule.
    today = date.today()
    counts = {c: 0 for c in COBERTURAS}
    rows = db.query(
        ClienteSysgalEstado.encontrado,
        ClienteSysgalEstado.estado_codigo,
        ClienteSysgalEstado.vigencia_hasta,
    ).all()
    for encontrado, codigo, hasta in rows:
        counts[derive_cobertura(codigo, hasta, encontrado, today)] += 1

    return SysgalStatusResponse(
        configured=configured,
        last_synced_at=last_synced_at,
        total_ruts=len(rows),
        por_cobertura=counts,
    )
