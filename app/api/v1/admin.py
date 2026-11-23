"""Admin-only operational endpoints (mounted at /api/v1/admin)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.services import estacion_reinicio

router = APIRouter()


class ReinicioSolicitado(BaseModel):
    solicitado_at: int
    pendiente: bool


class ReinicioEstado(BaseModel):
    solicitado_at: int | None = None
    hecho_at: int | None = None
    pendiente: bool


@router.post("/estacion/reinicio", response_model=ReinicioSolicitado)
def solicitar_reinicio(_: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Ask the scraping station to soft-reset; idempotent while a request is pending."""
    est = estacion_reinicio.solicitar(db, datetime.now(UTC))
    db.commit()
    return {"solicitado_at": est["solicitado_at"], "pendiente": est["pendiente"]}


@router.get("/estacion/reinicio", response_model=ReinicioEstado)
def estado_reinicio(_: str = Depends(require_admin), db: Session = Depends(get_db)):
    return estacion_reinicio.estado(db)
