"""Avance de cartera (admin only): Sysgal's cartera statistics, live and as a monthly snapshot.

Sysgal's numbers are passed through untouched; we only add ``lawyer_id`` / ``case_id`` links.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.config import settings
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import avance_cartera as svc
from app.services.sysgal_client import MAX_SIN_GESTION_PER_PAGE, SysgalClient, SysgalError

router = APIRouter()

MAX_RANGE_DAYS = 182
SYSGAL_DOWN = "Sysgal no respondió. Intenta de nuevo en unos minutos."


def get_sysgal_client() -> SysgalClient:
    return SysgalClient(settings.SYSGAL_BASE_URL, settings.SYSGAL_API_KEY)


def _check_range(desde: date, hasta: date) -> None:
    if desde > hasta:
        raise HTTPException(status_code=400, detail="La fecha desde debe ser anterior a hasta.")
    if (hasta - desde).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail="El rango máximo es de 6 meses.")


def _sysgal_down() -> HTTPException:
    return HTTPException(status_code=502, detail=SYSGAL_DOWN)


def _lawyer_by_sysgal(db: Session) -> dict[int, int]:
    rows = (
        db.query(SysgalUsuario.sysgal_id, SysgalUsuario.lawyer_id)
        .filter(SysgalUsuario.lawyer_id.isnot(None))
        .all()
    )
    return dict(rows)


def _periodo_or_400(periodo: str) -> None:
    try:
        svc.periodo_bounds(periodo)
    except ValueError:
        raise HTTPException(status_code=400, detail="periodo inválido (usa AAAA-MM)") from None


def _with_en_cartera(db: Session, por_abogado: list) -> list:
    """Copy of the rows with ``en_cartera`` added: mapped, active, non-admin lawyer."""
    ids = svc.cartera_lawyer_ids(db)
    return [{**row, "en_cartera": row.get("lawyer_id") in ids} for row in por_abogado]


def _snapshot_out(snap) -> dict:
    return {
        "periodo": snap.periodo,
        "desde": snap.desde,
        "hasta": snap.hasta,
        "taken_at": snap.taken_at,
        "taken_by": snap.taken_by,
        "payload": snap.payload,
    }


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
        body = client.estadisticas_cartera(desde, hasta)
    except SysgalError:
        raise _sysgal_down() from None
    lawyers = _lawyer_by_sysgal(db)
    body["por_abogado"] = _with_en_cartera(
        db, [{**row, "lawyer_id": lawyers.get(row.get("abogado_id"))} for row in body["por_abogado"]]
    )
    return body


@router.get("/sin-gestion")
async def sin_gestion(
    desde: date,
    hasta: date,
    abogado_id: Optional[int] = None,
    desde_id: Optional[int] = None,
    limite: int = Query(MAX_SIN_GESTION_PER_PAGE, ge=1, le=MAX_SIN_GESTION_PER_PAGE),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
    client: SysgalClient = Depends(get_sysgal_client),
):
    _check_range(desde, hasta)
    try:
        body = client.causas_sin_gestion(desde, hasta, abogado_id, desde_id, limite)
    except SysgalError:
        raise _sysgal_down() from None
    items = [row for row in body["data"] if isinstance(row, dict)]
    ids = [row.get("id_causa") for row in items if row.get("id_causa") is not None]
    case_by_causa: dict[int, int] = {}
    if ids:
        case_by_causa = dict(
            db.query(SysgalCausa.sysgal_id, SysgalCausa.case_id)
            .filter(SysgalCausa.sysgal_id.in_(ids), SysgalCausa.case_id.isnot(None))
            .all()
        )
    lawyers = _lawyer_by_sysgal(db)
    body["data"] = [
        {**row, "lawyer_id": lawyers.get(row.get("abogado_id")), "case_id": case_by_causa.get(row.get("id_causa"))}
        for row in items
    ]
    return body


@router.get("/snapshots/{periodo}")
async def get_snapshot(
    periodo: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    _periodo_or_400(periodo)
    snap = svc.get_snapshot(db, periodo)
    if snap is None:
        raise HTTPException(status_code=404, detail="No hay foto de ese mes.")
    out = _snapshot_out(snap)
    payload = out["payload"]
    if isinstance(payload, dict) and isinstance(payload.get("por_abogado"), list):
        lawyers = _lawyer_by_sysgal(db)
        rows = [
            {**row, "lawyer_id": row.get("lawyer_id", lawyers.get(row.get("abogado_id")))}
            for row in payload["por_abogado"]
        ]
        out["payload"] = {**payload, "por_abogado": _with_en_cartera(db, rows)}
    return out


@router.post("/snapshots/{periodo}")
async def take_snapshot(
    periodo: str,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
    client: SysgalClient = Depends(get_sysgal_client),
):
    _periodo_or_400(periodo)
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    try:
        snap = svc.take_snapshot(db, client, periodo, taken_by=admin.id if admin else None)
    except svc.PeriodoNoTerminado:
        raise HTTPException(status_code=400, detail="El mes aún no termina.") from None
    except svc.PeriodoCerrado:
        raise HTTPException(status_code=409, detail="La liquidación de ese mes ya está cerrada.") from None
    except SysgalError:
        raise _sysgal_down() from None
    return _snapshot_out(snap)
