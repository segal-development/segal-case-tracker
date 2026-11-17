"""External read-only ``GET /patrocinio`` endpoint for the Sysgal CRM.

Patrocinio/poder status per Sysgal causa (pull, ``actualizado_desde``). Keyed by Sysgal causa id
(``sysgal_causa_id``, repeatable or comma list) and/or ``rol`` + ``tribunal``; with neither, every
Sysgal causa. State derivation lives in ``app.services.patrocinio_estado``.

``bloqueado_por_mora`` is ALWAYS ``false`` for now: the cuotas source (ITDS cuotas endpoint,
tasks T1/T2) does not exist yet. Sysgal itself knows who is moroso and files the cobro request
when it sees ``poder_pendiente`` with the block.

Also restricted by ``SYSGAL_API_ALLOWED_IPS`` (defense in depth; the API key is the real gate).
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal.deps import require_sysgal_allowed_ip, require_sysgal_key
from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa
from app.services.patrocinio_estado import derive_patrocinio_estado
from app.services.sysgal_matching import normalize_rol

router = APIRouter()


class SysgalPatrocinioItem(BaseModel):
    sysgal_causa_id: int
    case_id: Optional[int] = None
    rol: str
    tribunal: str
    estado: str
    bloqueado_por_mora: bool = False
    poder_acreditado_at: Optional[datetime] = None
    escrito_presentado_at: Optional[datetime] = None
    actualizado_at: Optional[datetime] = None


class SysgalPatrocinioResponse(BaseModel):
    total: int
    page: int
    per_page: int
    items: List[SysgalPatrocinioItem]


def _parse_ids(values: List[str]) -> List[int]:
    ids: List[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid sysgal_causa_id: {part!r}")
    return ids


@router.get("/patrocinio", response_model=SysgalPatrocinioResponse)
def list_patrocinio(
    sysgal_causa_id: List[str] = Query(
        default=[], description="Sysgal causa id(s): repeatable or comma-separated"
    ),
    rol: Optional[str] = Query(None, description="ROL (requires tribunal)"),
    tribunal: Optional[str] = Query(None, description="Tribunal name (requires rol)"),
    actualizado_desde: Optional[datetime] = Query(
        None, description="ISO datetime; only causas updated at/after it"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _ip=Depends(require_sysgal_allowed_ip),
    _key=Depends(require_sysgal_key),
) -> SysgalPatrocinioResponse:
    """Patrocinio/poder state per Sysgal causa, ordered by ``sysgal_causa_id``.

    ``actualizado_at`` = latest of the causa's ``synced_at`` / ``sysgal_updated_at`` and, when
    linked, the case's ``updated_at`` / ``last_movement_at``.
    """
    if (rol is None) != (tribunal is None):
        raise HTTPException(status_code=422, detail="rol and tribunal must be sent together")
    ids = _parse_ids(sysgal_causa_id)

    q = db.query(SysgalCausa, Case).outerjoin(Case, Case.id == SysgalCausa.case_id)
    if ids:
        q = q.filter(SysgalCausa.sysgal_id.in_(ids))
    if rol is not None and tribunal is not None:
        q = q.filter(
            SysgalCausa.rol_norm == normalize_rol(rol),
            func.lower(SysgalCausa.tribunal) == tribunal.strip().lower(),
        )

    def _updated(causa: SysgalCausa, case: Optional[Case]) -> Optional[datetime]:
        stamps = [causa.synced_at, causa.sysgal_updated_at]
        if case is not None:
            stamps += [case.updated_at, case.last_movement_at]
        stamps = [s.replace(tzinfo=None) for s in stamps if s is not None]
        return max(stamps) if stamps else None

    since = actualizado_desde.replace(tzinfo=None) if actualizado_desde else None
    rows = []
    for causa, case in q.order_by(SysgalCausa.sysgal_id).all():
        updated = _updated(causa, case)
        if since is not None and (updated is None or updated < since):
            continue
        rows.append((causa, case, updated))

    total = len(rows)
    page_rows = rows[(page - 1) * per_page : page * per_page]
    items = []
    for causa, case, updated in page_rows:
        derived = derive_patrocinio_estado(db, case)
        items.append(
            SysgalPatrocinioItem(
                sysgal_causa_id=causa.sysgal_id,
                case_id=case.id if case else None,
                rol=causa.rol,
                tribunal=causa.tribunal,
                estado=derived.estado,
                bloqueado_por_mora=False,
                poder_acreditado_at=derived.poder_acreditado_at,
                escrito_presentado_at=derived.escrito_presentado_at,
                actualizado_at=updated,
            )
        )
    return SysgalPatrocinioResponse(total=total, page=page, per_page=per_page, items=items)
