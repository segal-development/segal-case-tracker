"""External read-only ``GET /plazos`` endpoint for the Sysgal CRM.

Agenda of upcoming/overdue DEADLINES across the client's ACTIVE causas,
sorted by date ascending. Mirrors the internal ``/api/v1/calendar`` windowing
semantics (``_today_chile``, the ``[today, today+days]`` inclusive window, and
the overdue rule: a past-due deadline is included only when still actionable —
semaforo not in {verde, gris}). Read-only fan-out over the ``next_deadline_at``
column the DeadlineEngine already denormalizes onto ``Case``; no computation.

Bounded field set — no internal ids, no lawyer attribution.
"""

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal._scope import active_case_ids_for_cliente
from app.api.sysgal.deps import require_sysgal_key
from app.models.case import Case
from app.models.court import Court
from app.services.deadline_engine import _today_chile

router = APIRouter()


class SysgalPlazoItem(BaseModel):
    """Bounded, read-only view of a procedural deadline for the Sysgal CRM."""

    rol: str
    caratulado: str
    proximo_plazo: date
    fatal: bool
    overdue: bool
    semaforo: Optional[str] = None
    tribunal: Optional[str] = None


def _is_still_active(semaforo: Optional[str]) -> bool:
    """A past-due deadline is still actionable unless the case is cleared /
    not applicable — mirrors calendar's rule: semaforo not in {verde, gris}."""
    return semaforo not in ("verde", "gris")


@router.get("/plazos", response_model=List[SysgalPlazoItem])
def list_plazos(
    cliente_rut: str = Query(..., description="RUT del cliente (litigante) a consultar"),
    days: int = Query(30, ge=1, description="Horizonte en días desde hoy (inclusive)"),
    include_overdue: bool = Query(
        True, description="Incluir también plazos vencidos que sigan activos"
    ),
    db: Session = Depends(get_db),
    _key=Depends(require_sysgal_key),
) -> List[SysgalPlazoItem]:
    """Agenda of deadlines for the client's ACTIVE causas.

    A case contributes its ``next_deadline_at`` when that date falls inside the
    ``[today, today+days]`` window, or (when ``include_overdue``) when it is
    past-due and the case is still actionable. Sorted by date ascending.
    """
    case_ids = active_case_ids_for_cliente(db, cliente_rut)
    if not case_ids:
        return []

    today = _today_chile()
    horizon_end = today + timedelta(days=days)

    cases = (
        db.query(Case)
        .filter(Case.id.in_(case_ids), Case.next_deadline_at.isnot(None))
        .all()
    )

    court_ids = {c.court_id for c in cases if c.court_id is not None}
    court_names = {}
    if court_ids:
        court_names = {
            cid: name
            for cid, name in db.query(Court.id, Court.name)
            .filter(Court.id.in_(court_ids))
            .all()
        }

    items: List[SysgalPlazoItem] = []
    for case in cases:
        due = case.next_deadline_at
        if today <= due <= horizon_end:
            overdue = False
        elif due < today and include_overdue and _is_still_active(case.semaforo):
            overdue = True
        else:
            continue

        items.append(
            SysgalPlazoItem(
                rol=case.rol,
                caratulado=f"{case.plaintiff or ''}/{case.defendant or ''}",
                proximo_plazo=due,
                fatal=bool(case.next_deadline_fatal),
                overdue=overdue,
                semaforo=case.semaforo,
                tribunal=court_names.get(case.court_id),
            )
        )

    items.sort(key=lambda i: (i.proximo_plazo, i.rol))
    return items
