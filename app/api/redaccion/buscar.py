"""External read-only ``GET /buscar`` endpoint for the Redaccion system.

Locates causas the drafter wants to work on, either by ROL/RIT or by client
RUT. At least one of ``rol`` / ``rut`` is required (400 otherwise). Returns a
BOUNDED, paginated list whose ``case_id`` is then passed to
``GET /causas/{case_id}/detalle`` to pull the full drafting bundle.

Read-only. No documents/files/download links are ever exposed.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.redaccion.deps import require_redaccion_key
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.utils.rut import normalize_rut

router = APIRouter()


class RedaccionCausaItem(BaseModel):
    """Bounded search hit for the Redaccion system.

    Unlike the Sysgal API (which hides all ids), ``case_id`` IS exposed here on
    purpose: it is the handle the drafter passes to the detalle endpoint.
    """

    case_id: int
    rol: str
    rit: Optional[str] = None
    caratulado: str
    materia: Optional[str] = None
    procedimiento: Optional[str] = None
    tribunal: Optional[str] = None
    estado: Optional[str] = None
    semaforo: Optional[str] = None
    proximo_plazo: Optional[date] = None


class RedaccionCausaListResponse(BaseModel):
    """Paginated list of causas matching the search."""

    total: int
    page: int
    per_page: int
    items: List[RedaccionCausaItem]


@router.get("/buscar", response_model=RedaccionCausaListResponse)
def buscar_causas(
    rol: Optional[str] = Query(
        None, description="ROL o RIT de la causa (coincidencia parcial, case-insensitive)"
    ),
    rut: Optional[str] = Query(
        None, description="RUT del cliente/litigante (cualquier rol de participante)"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _key=Depends(require_redaccion_key),
) -> RedaccionCausaListResponse:
    """Search causas by ROL/RIT and/or client RUT.

    At least one of ``rol`` / ``rut`` must be provided (400 otherwise).

      - ``rol``: case-insensitive partial match on ``Case.rol`` OR ``Case.rit``.
      - ``rut``: normalized, matched against any ``CaseLitigante.rut`` (any
        participante role) on the case.

    Both may be combined (AND). Paginated and ordered by rol for determinism.
    """
    rol_term = rol.strip() if rol else ""
    rut_term = rut.strip() if rut else ""

    if not rol_term and not rut_term:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe indicar al menos uno de: 'rol' o 'rut'.",
        )

    query = db.query(Case)

    if rut_term:
        normalized = normalize_rut(rut_term)
        query = query.join(CaseLitigante, CaseLitigante.case_id == Case.id).filter(
            CaseLitigante.rut == normalized
        )

    if rol_term:
        like = f"%{rol_term}%"
        query = query.filter(or_(Case.rol.ilike(like), Case.rit.ilike(like)))

    query = query.distinct()

    total = query.count()

    rows = (
        query.order_by(Case.rol)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Resolve tribunal names in one pass (no per-row N+1).
    court_ids = {r.court_id for r in rows if r.court_id is not None}
    court_names: dict = {}
    if court_ids:
        court_names = {
            cid: name
            for cid, name in db.query(Court.id, Court.name)
            .filter(Court.id.in_(court_ids))
            .all()
        }

    items = [
        RedaccionCausaItem(
            case_id=r.id,
            rol=r.rol,
            rit=r.rit,
            caratulado=f"{r.plaintiff or ''}/{r.defendant or ''}",
            materia=r.matter,
            procedimiento=r.procedure,
            tribunal=court_names.get(r.court_id),
            estado=r.status,
            semaforo=r.semaforo,
            proximo_plazo=r.next_deadline_at,
        )
        for r in rows
    ]

    return RedaccionCausaListResponse(
        total=total, page=page, per_page=per_page, items=items
    )
