"""In-app alert feed endpoints.

Alerts (`app.models.alert.Alert`) were previously write-only from the app's
perspective: rows are created by the sync/deadline/observability pipelines
and delivered via email/webhook, but there was no way for a lawyer to read
their own feed in-app. These endpoints add that read/dismiss surface.

Every endpoint here is scoped to the CALLING lawyer's own alerts only
(`Alert.lawyer_id == _resolve_lawyer_id(...)`) — alerts are per-recipient,
so a lawyer only ever sees or mutates their own, never another lawyer's.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, _resolve_lawyer_id
from app.models.alert import Alert
from app.models.case import Case

router = APIRouter()


# ============================================================================
# RESPONSE SCHEMAS
# ============================================================================


class AlertItem(BaseModel):
    """Single alert row, enriched with lightweight case context for display."""

    id: int
    type: str
    title: str
    message: Optional[str] = None
    case_id: int
    created_at: Optional[datetime] = None
    read: bool
    read_at: Optional[datetime] = None
    # Case context for display. All optional/None-safe: the case may have
    # been archived or removed by the time the alert is read.
    case_rol: Optional[str] = None
    case_caratulado: Optional[str] = None
    case_court_name: Optional[str] = None

    class Config:
        from_attributes = True


class AlertListResponse(BaseModel):
    """Paginated alert feed for the current lawyer."""

    items: List[AlertItem]
    total: int
    page: int
    per_page: int
    pages: int
    unread_count: int


class MarkAllReadResponse(BaseModel):
    marked: int


# ============================================================================
# HELPERS
# ============================================================================


def _build_alert_item(alert: Alert, case: Optional[Case]) -> AlertItem:
    case_rol = None
    case_caratulado = None
    case_court_name = None
    if case is not None:
        case_rol = case.rol
        case_caratulado = f"{case.plaintiff or ''}/{case.defendant or ''}"
        if case.court is not None:
            case_court_name = case.court.name

    return AlertItem(
        id=alert.id,
        type=alert.type,
        title=alert.title,
        message=alert.message,
        case_id=alert.case_id,
        created_at=alert.created_at,
        read=bool(alert.read),
        read_at=alert.read_at,
        case_rol=case_rol,
        case_caratulado=case_caratulado,
        case_court_name=case_court_name,
    )


# ============================================================================
# ENDPOINTS
# ============================================================================


@router.get("", response_model=AlertListResponse)
async def list_alerts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List the current lawyer's own alerts, most-recent-first, paginated.

    ``unread_count`` always reflects the lawyer's TOTAL unread alerts,
    regardless of the ``unread_only`` filter or the current page.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    base_query = db.query(Alert).filter(Alert.lawyer_id == lawyer_id)

    unread_count = base_query.filter(Alert.read.is_(False)).count()

    query = base_query
    if unread_only:
        query = query.filter(Alert.read.is_(False))

    total = query.count()

    alerts = (
        query.order_by(Alert.created_at.desc(), Alert.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    case_ids = {a.case_id for a in alerts if a.case_id is not None}
    cases_by_id = {}
    if case_ids:
        cases_by_id = {
            c.id: c for c in db.query(Case).filter(Case.id.in_(case_ids)).all()
        }

    items = [
        _build_alert_item(alert, cases_by_id.get(alert.case_id))
        for alert in alerts
    ]

    pages = (total + per_page - 1) // per_page if total > 0 else 0

    return AlertListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        unread_count=unread_count,
    )


@router.post("/{alert_id}/read", response_model=AlertItem)
async def mark_alert_read(
    alert_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Mark one of the caller's OWN alerts as read. Idempotent.

    404s both when the alert doesn't exist and when it belongs to another
    lawyer, so existence isn't leaked.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    alert = (
        db.query(Alert)
        .filter(Alert.id == alert_id, Alert.lawyer_id == lawyer_id)
        .first()
    )
    if not alert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found"
        )

    if not alert.read:
        alert.read = True
        alert.read_at = datetime.utcnow()
        db.commit()
        db.refresh(alert)

    case = db.query(Case).filter(Case.id == alert.case_id).first()
    return _build_alert_item(alert, case)


@router.post("/read-all", response_model=MarkAllReadResponse)
async def mark_all_alerts_read(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Mark ALL of the caller's unread alerts as read. Returns the count marked."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    now = datetime.utcnow()
    marked = (
        db.query(Alert)
        .filter(Alert.lawyer_id == lawyer_id, Alert.read.is_(False))
        .update({Alert.read: True, Alert.read_at: now}, synchronize_session=False)
    )
    db.commit()

    return MarkAllReadResponse(marked=marked)
