"""Escritos generados en Sysgal (admin only): GEDOC statistics and detail list.

Sysgal's numbers are passed through untouched; we only add ``lawyer_id`` / ``case_id`` links.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.config import settings
from app.services import escritos_sysgal as svc
from app.services.sysgal_client import MAX_ESCRITOS_PER_PAGE, SysgalClient, SysgalError

router = APIRouter()

SYSGAL_DOWN = "Sysgal no respondió. Intenta de nuevo en unos minutos."
DEFAULT_LIMITE = 100


def get_sysgal_client() -> SysgalClient:
    return SysgalClient(settings.SYSGAL_BASE_URL, settings.SYSGAL_API_KEY)


def _check_range(desde: date, hasta: date) -> None:
    try:
        svc.check_range(desde, hasta)
    except svc.RangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/estadisticas")
async def estadisticas(
    desde: date,
    hasta: date,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
    client: SysgalClient = Depends(get_sysgal_client),
):
    _check_range(desde, hasta)
    try:
        body = client.escritos_estadisticas(desde, hasta)
    except SysgalError:
        raise HTTPException(status_code=502, detail=SYSGAL_DOWN) from None
    return svc.enrich_stats(db, body)


@router.get("/listado")
async def listado(
    desde: date,
    hasta: date,
    abogado_id: Optional[int] = None,
    tipo_id: Optional[str] = None,
    desde_id: Optional[int] = None,
    limite: int = Query(DEFAULT_LIMITE, ge=1, le=MAX_ESCRITOS_PER_PAGE),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
    client: SysgalClient = Depends(get_sysgal_client),
):
    _check_range(desde, hasta)
    try:
        body = client.escritos(desde, hasta, abogado_id, tipo_id, desde_id, limite)
    except SysgalError:
        raise HTTPException(status_code=502, detail=SYSGAL_DOWN) from None
    return svc.enrich_page(db, body)
