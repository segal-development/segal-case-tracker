"""External read-only ``GET /patrocinio`` endpoint for the Sysgal CRM.

Patrocinio/poder status per Sysgal causa (pull, ``actualizado_desde``). Keyed by Sysgal causa id
(``sysgal_causa_id``, repeatable or comma list) and/or ``rol`` + ``tribunal``; with neither, every
Sysgal causa. State derivation lives in ``app.services.patrocinio_estado``.

``bloqueado_por_mora`` is true when the case is blocked by mora (``app.services.sysgal_cuotas``:
a contract listing one of its causas is moroso on cuota 1 or 2) and the estado is
``escrito_presentado`` or ``poder_pendiente``. Unknown mora (no contract info) is NOT blocked.
``cuotas_actualizadas_al`` is the date of Sysgal's cuotas copy (null if never synced). Sysgal
files the cobro request when it sees ``poder_pendiente`` with the block.

Only reported while the goals switch ``sysgal_bloqueo_por_mora_activo`` is 1 (default 0: always false).

Also restricted by ``SYSGAL_API_ALLOWED_IPS`` (defense in depth; the API key is the real gate).
"""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_serializer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal.deps import require_sysgal_allowed_ip, require_sysgal_key
from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa
from app.services.app_settings import (
    DEFAULT_SYSGAL_BLOQUEO_POR_MORA,
    KEY_SYSGAL_BLOQUEO_POR_MORA,
    get_int,
)
from app.services.patrocinio_estado import bloqueado_por_mora, derive_patrocinio_estado
from app.services.sysgal_cuotas import cuotas_actualizadas_al, load_mora_por_case
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
    cuotas_actualizadas_al: Optional[datetime] = None

    @field_serializer(
        "poder_acreditado_at", "escrito_presentado_at", "actualizado_at", "cuotas_actualizadas_al"
    )
    def _utc_z(self, value: Optional[datetime]) -> Optional[str]:
        """Stored datetimes are naive UTC: emit them as explicit UTC (``...Z``)."""
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"


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
        None,
        description="ISO datetime; only causas updated at/after it. Naive = UTC; offsets are converted",
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

    # Causas deleted in Sysgal are not reported.
    q = (
        db.query(SysgalCausa, Case)
        .outerjoin(Case, Case.id == SysgalCausa.case_id)
        .filter(SysgalCausa.eliminada.is_(False))
    )
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

    since = actualizado_desde
    if since is not None and since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)  # convert, don't drop the offset
    rows = []
    for causa, case in q.order_by(SysgalCausa.sysgal_id).all():
        updated = _updated(causa, case)
        if since is not None and (updated is None or updated < since):
            continue
        rows.append((causa, case, updated))

    total = len(rows)
    page_rows = rows[(page - 1) * per_page : page * per_page]
    # S10: while the switch is OFF, ITDS always sees bloqueado_por_mora=false.
    bloqueo_activo = get_int(db, KEY_SYSGAL_BLOQUEO_POR_MORA, DEFAULT_SYSGAL_BLOQUEO_POR_MORA) == 1
    mora = (
        load_mora_por_case(db, [case.id for _, case, _ in page_rows if case is not None])
        if bloqueo_activo
        else {}
    )
    cuotas_al = cuotas_actualizadas_al(db)
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
                bloqueado_por_mora=bloqueado_por_mora(derived.estado, mora.get(case.id) if case else None),
                poder_acreditado_at=derived.poder_acreditado_at,
                escrito_presentado_at=derived.escrito_presentado_at,
                actualizado_at=updated,
                cuotas_actualizadas_al=cuotas_al,
            )
        )
    return SysgalPatrocinioResponse(total=total, page=page, per_page=per_page, items=items)
