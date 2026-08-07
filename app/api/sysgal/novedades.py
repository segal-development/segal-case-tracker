"""External read-only ``GET /novedades`` endpoint for the Sysgal CRM.

Recent MOVEMENTS (actuaciones) across the client's ACTIVE causas within the
last ``days``, newest first, capped at ``limit``. Read-only fan-out over the
``movements`` table scoped to the client's active cases.

Bounded field set — no internal ids, no lawyer attribution.
"""

from datetime import date, datetime, time, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal._scope import active_case_ids_for_cliente
from app.api.sysgal.deps import require_sysgal_key
from app.models.case import Case
from app.models.court import Court
from app.models.movement import Movement
from app.services.deadline_engine import _today_chile

router = APIRouter()

_DESC_MAX = 300


class SysgalNovedadItem(BaseModel):
    """Bounded, read-only view of a case movement for the Sysgal CRM."""

    rol: str
    fecha: date
    etapa: Optional[str] = None
    tramite: Optional[str] = None
    descripcion: Optional[str] = None
    tribunal: Optional[str] = None


def _truncate(text: Optional[str], limit: int = _DESC_MAX) -> Optional[str]:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


@router.get("/novedades", response_model=List[SysgalNovedadItem])
def list_novedades(
    cliente_rut: str = Query(..., description="RUT del cliente (litigante) a consultar"),
    days: int = Query(30, ge=1, description="Ventana hacia atrás en días (inclusive)"),
    limit: int = Query(50, ge=1, le=200, description="Máximo de novedades (tope 200)"),
    db: Session = Depends(get_db),
    _key=Depends(require_sysgal_key),
) -> List[SysgalNovedadItem]:
    """Recent movements for the client's ACTIVE causas.

    Includes movements with ``movement_date >= today - days``, ordered newest
    first, capped at ``limit`` (max 200).
    """
    case_ids = active_case_ids_for_cliente(db, cliente_rut)
    if not case_ids:
        return []

    cutoff = datetime.combine(_today_chile() - timedelta(days=days), time.min)

    rows = (
        db.query(Movement, Case.rol, Case.court_id)
        .join(Case, Case.id == Movement.case_id)
        .filter(Movement.case_id.in_(case_ids), Movement.movement_date >= cutoff)
        .order_by(Movement.movement_date.desc(), Movement.id.desc())
        .limit(limit)
        .all()
    )

    court_ids = {court_id for _, _, court_id in rows if court_id is not None}
    court_names = {}
    if court_ids:
        court_names = {
            cid: name
            for cid, name in db.query(Court.id, Court.name)
            .filter(Court.id.in_(court_ids))
            .all()
        }

    def _as_date(value) -> date:
        return value.date() if isinstance(value, datetime) else value

    return [
        SysgalNovedadItem(
            rol=rol,
            fecha=_as_date(mv.movement_date),
            etapa=mv.stage,
            tramite=mv.procedure,
            descripcion=_truncate(mv.description),
            tribunal=court_names.get(court_id),
        )
        for mv, rol, court_id in rows
    ]
