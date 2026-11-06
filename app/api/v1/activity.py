"""Activity audit API: record page views (any user), read the trail (admin only)."""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, require_admin
from app.models.activity_event import ActivityEvent
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.services import activity_audit
from app.services.activity_labels import label_for

router = APIRouter()

CHILE = ZoneInfo("America/Santiago")
DEDUPE_SECONDS = 30


class ViewBody(BaseModel):
    # A path only: no query string or fragment (they may carry tokens).
    path: str = Field(..., max_length=200, pattern=r"^/[^?#]*$")
    case_id: Optional[int] = None


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat() + "Z"


def _chile_day_bounds(day: date):
    """[start, end) of a Chile-local day as naive UTC datetimes."""
    start = datetime(day.year, day.month, day.day, tzinfo=CHILE)
    end = datetime(day.year, day.month, day.day, tzinfo=CHILE) + timedelta(days=1)
    # Re-anchor the end at local midnight of the next day (DST-safe).
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=CHILE)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(end)


@router.post("/views", status_code=204)
def record_view(
    body: ViewBody,
    payload: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    sub = payload.get("sub") or payload.get("lawyer_id")
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        lawyer = db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    else:
        lawyer = db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()
    try:
        lawyer_id = lawyer.id if lawyer else None
        since = datetime.utcnow() - timedelta(seconds=DEDUPE_SECONDS)
        recent = (
            db.query(ActivityEvent.id)
            .filter(
                ActivityEvent.kind == "view",
                ActivityEvent.lawyer_id == lawyer_id,
                ActivityEvent.route == body.path,
                ActivityEvent.created_at >= since,
            )
            .first()
        )
        if recent is None:
            activity_audit.insert_event(
                db, kind="view", route=body.path, path=body.path, case_id=body.case_id,
                **activity_audit.actor_fields(lawyer),
            )
    except Exception:  # S3: never fail because of the audit
        db.rollback()
    return Response(status_code=204)


@router.get("")
def list_activity(
    lawyer_id: Optional[int] = None,
    kind: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    rol: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(ActivityEvent, Case.rol).outerjoin(Case, Case.id == ActivityEvent.case_id)
    if lawyer_id is not None:
        q = q.filter(ActivityEvent.lawyer_id == lawyer_id)
    if kind:
        q = q.filter(ActivityEvent.kind == kind)
    if desde:
        q = q.filter(ActivityEvent.created_at >= _chile_day_bounds(desde)[0])
    if hasta:
        q = q.filter(ActivityEvent.created_at < _chile_day_bounds(hasta)[1])
    if rol:
        q = q.filter(Case.rol.ilike(f"%{rol}%"))
    total = q.count()
    rows = (
        q.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    items = [
        {
            "id": e.id,
            "created_at": _iso(e.created_at),
            "lawyer_id": e.lawyer_id,
            "actor_name": e.actor_name,
            "actor_role": e.actor_role,
            "kind": e.kind,
            "method": e.method,
            "route": e.route,
            "path": e.path,
            "status_code": e.status_code,
            "case_id": e.case_id,
            "rol": case_rol,
            "target": e.target,
            "label": label_for(e.kind, e.method, e.route),
            "ip": e.ip,
        }
        for e, case_rol in rows
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / per_page)),
    }


@router.get("/summary")
def activity_summary(
    dia: Optional[date] = None,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    day = dia or datetime.now(CHILE).date()
    start, end = _chile_day_bounds(day)
    events = (
        db.query(ActivityEvent)
        .filter(
            ActivityEvent.lawyer_id.isnot(None),
            ActivityEvent.created_at >= start,
            ActivityEvent.created_at < end,
        )
        .order_by(ActivityEvent.created_at)
        .all()
    )
    people: dict = {}
    for e in events:
        p = people.setdefault(
            e.lawyer_id,
            {
                "lawyer_id": e.lawyer_id, "actor_name": e.actor_name, "actor_role": e.actor_role,
                "first_at": e.created_at, "last_at": e.created_at,
                "views": 0, "actions": 0, "_cases": set(),
            },
        )
        p["last_at"] = e.created_at
        if e.kind == "view":
            p["views"] += 1
            if e.case_id is not None:
                p["_cases"].add(e.case_id)
        elif e.kind == "action":
            p["actions"] += 1
    out = []
    for p in sorted(people.values(), key=lambda x: x["last_at"], reverse=True):
        out.append(
            {
                "lawyer_id": p["lawyer_id"], "actor_name": p["actor_name"],
                "actor_role": p["actor_role"],
                "first_at": _iso(p["first_at"]), "last_at": _iso(p["last_at"]),
                "views": p["views"], "actions": p["actions"],
                "cases_viewed": len(p["_cases"]),
            }
        )
    return out
