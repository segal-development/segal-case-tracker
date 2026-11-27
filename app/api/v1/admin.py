"""Admin-only operational endpoints (mounted at /api/v1/admin)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.lawyer import Lawyer
from app.models.pjud_request_count import PjudRequestCount
from app.services import app_settings, estacion_reinicio
from app.services.egress_guard import parse_entry
from app.services.pjud_metrics import KIND_REQUEST

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


class IpsPermitidas(BaseModel):
    ips: list[str]


def _read_ips(db: Session) -> list[str]:
    raw = app_settings.get_text(db, app_settings.KEY_IPS_PERMITIDAS, "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


@router.get("/estacion/ips-permitidas", response_model=IpsPermitidas)
def get_ips_permitidas(_: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Egress allow-list the station reads (DB value; empty = allow-list off unless env sets one)."""
    return {"ips": _read_ips(db)}


@router.put("/estacion/ips-permitidas", response_model=IpsPermitidas)
def put_ips_permitidas(
    body: IpsPermitidas, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    """Replace the allow-list. Every entry must be an IP or CIDR; otherwise 422 and nothing is saved."""
    entries: list[str] = []
    for entry in body.ips:
        entry = entry.strip()
        net = None if "," in entry else parse_entry(entry, strict=True)
        if net is None or net.prefixlen == 0:
            raise HTTPException(status_code=422, detail=f"Entrada inválida (IP o CIDR esperado): {entry!r}")
        if entry not in entries:
            entries.append(entry)
    app_settings.set_text(db, app_settings.KEY_IPS_PERMITIDAS, ",".join(entries))
    db.commit()
    return {"ips": entries}


@router.get("/pjud/actividad")
def pjud_actividad(
    days: int = Query(7, ge=1, le=90),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """PJUD footprint of the station: requests and rejections over the last *days*.

    Admin only because it carries egress IPs. Counts come from ``pjud_request_counts``.
    """
    since = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0) - timedelta(days=days)
    rows = (
        db.query(PjudRequestCount)
        .filter(PjudRequestCount.hour_utc >= since)
        .order_by(PjudRequestCount.hour_utc)
        .all()
    )
    names = {i: n for i, n in db.query(Lawyer.id, Lawyer.name).all()}

    def bump(bucket: dict, key, row) -> None:
        entry = bucket.setdefault(key, {"requests": 0, "rejections": 0})
        entry["requests" if row.kind == KIND_REQUEST else "rejections"] += row.count

    per_day: dict = {}
    per_hour: dict = {}
    by_ip: dict = {}
    by_action: dict = {}
    by_account: dict = {}
    rejections: dict = {}
    for r in rows:
        bump(per_day, r.hour_utc.date().isoformat(), r)
        bump(per_hour, r.hour_utc.isoformat(), r)
        bump(by_ip, r.egress_ip, r)
        bump(by_action, r.action, r)
        bump(by_account, r.lawyer_id, r)
        if r.kind != KIND_REQUEST:
            k = (r.kind, r.egress_ip, r.action, r.lawyer_id)
            rejections[k] = rejections.get(k, 0) + r.count

    return {
        "days": days,
        "total_requests": sum(v["requests"] for v in per_day.values()),
        "total_rejections": sum(v["rejections"] for v in per_day.values()),
        "per_day": [{"day": k, **v} for k, v in sorted(per_day.items())],
        "per_hour": [{"hour_utc": k, **v} for k, v in sorted(per_hour.items())],
        "by_ip": [{"egress_ip": k, **v} for k, v in sorted(by_ip.items())],
        "by_action": [{"action": k, **v} for k, v in sorted(by_action.items())],
        "by_account": [
            {"lawyer_id": k, "name": names.get(k), **v}
            for k, v in sorted(by_account.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ],
        "rejections": [
            {"kind": k[0], "egress_ip": k[1], "action": k[2], "lawyer_id": k[3], "count": c}
            for k, c in sorted(rejections.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3] or 0))
        ],
    }
