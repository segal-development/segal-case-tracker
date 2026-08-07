"""External read-only ``GET /causas`` endpoint for the Sysgal CRM.

Returns ONLY active causas where the given client RUT appears as a litigante
(any party role). Exposes a BOUNDED, non-sensitive field set — no internal
ids, no lawyer attribution, no credentials, no raw litigante PII beyond the
caratulado already public on PJUD.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal.deps import require_sysgal_key
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.utils.rut import normalize_rut

router = APIRouter()


class SysgalCausaItem(BaseModel):
    """Bounded, read-only view of a causa for the Sysgal CRM."""

    rol: str
    caratulado: str
    materia: Optional[str] = None
    estado: Optional[str] = None
    semaforo: Optional[str] = None
    proximo_plazo: Optional[date] = None
    tribunal: Optional[str] = None
    competencia: Optional[str] = None


class SysgalCausaListResponse(BaseModel):
    """Paginated list of causas for a client RUT."""

    total: int
    page: int
    per_page: int
    items: List[SysgalCausaItem]


@router.get("/causas", response_model=SysgalCausaListResponse)
def list_causas(
    cliente_rut: str = Query(..., description="RUT del cliente (litigante) a consultar"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _key=Depends(require_sysgal_key),
) -> SysgalCausaListResponse:
    """List the ACTIVE causas where ``cliente_rut`` is a litigante.

    Read-only. Matches the client by normalized RUT against any
    ``CaseLitigante`` row, then returns only cases with ``status == "active"``
    (archived/closed excluded). Paginated, ordered by rol for determinism.
    """
    rut = normalize_rut(cliente_rut)

    base = (
        db.query(Case)
        .join(CaseLitigante, CaseLitigante.case_id == Case.id)
        .filter(CaseLitigante.rut == rut, Case.status == "active")
        .distinct()
    )

    total = base.count()

    rows = (
        base.order_by(Case.rol)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Resolve tribunal names in one pass (bounded field, no id exposure).
    court_ids = {r.court_id for r in rows if r.court_id is not None}
    court_names = {}
    if court_ids:
        court_names = {
            cid: name
            for cid, name in db.query(Court.id, Court.name)
            .filter(Court.id.in_(court_ids))
            .all()
        }

    items = [
        SysgalCausaItem(
            rol=r.rol,
            caratulado=f"{r.plaintiff or ''}/{r.defendant or ''}",
            materia=r.matter,
            estado=r.status,
            semaforo=r.semaforo,
            proximo_plazo=r.next_deadline_at,
            tribunal=court_names.get(r.court_id),
            competencia=r.competencia,
        )
        for r in rows
    ]

    return SysgalCausaListResponse(
        total=total, page=page, per_page=per_page, items=items
    )
