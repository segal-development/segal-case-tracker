"""In-app alert feed endpoints.

Alerts (`app.models.alert.Alert`) were previously write-only from the app's
perspective: rows are created by the sync/deadline/observability pipelines
and delivered via email/webhook, but there was no way for a lawyer to read
their own feed in-app. These endpoints add that read/dismiss surface.

Every endpoint here is scoped to the CALLING lawyer's own alerts only
(`Alert.lawyer_id == _resolve_lawyer_id(...)`) — alerts are per-recipient,
so a lawyer only ever sees or mutates their own, never another lawyer's.
"""

from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_lawyer, get_db, _resolve_lawyer_id
from app.models.alert import Alert
from app.models.case import Case
from app.services import alert_groups

router = APIRouter()


# ============================================================================
# ALERT CATEGORIES
# ============================================================================

# Single source of truth for which alert types are "actionable" (require a
# lawyer's attention/decision) vs. plain "activity" (high-volume informational
# noise: new movements, notificaciones, exhortos, escritos). Any alert type
# NOT in this set is treated as activity, including future types we haven't
# thought of yet.
ACTIONABLE_ALERT_TYPES = frozenset(
    {
        "semaforo_rojo",
        "deadline_fatal",
        "deadline_added",
        "deadline_audit",
        "credential_change",
        "status_change",
        # Fatal "ratificar firma" plazo that is provably the firm's own.
        "ratificar_firma",
        # Plazo semaforo (amarillo / rojo): immediate, one per plazo, never part
        # of a digest. The grouped "plazo_verde" is deliberately NOT here: it is
        # activity, announced once a day in one email.
        "plazo_amarillo",
        "plazo_rojo",
        # Fatal plazo that expired with no filing from the firm (one per plazo).
        "plazo_vencido",
        # Fase 0: fatal plazo lost with no writ, escalated to the lawyer and the admins.
        "plazo_fatal_vencido_escalado",
        # Decision on the lawyer's own liberación request (in-app only, no e-mail).
        "liberacion_aprobada",
        "liberacion_rechazada",
    }
)

# Alertas cuyo texto/relevancia depende del plazo de la causa. Una causa cuyo
# plazo venció hace mucho ya no es "acción del día" → sale del feed accionable (B).
DEADLINE_ALERT_TYPES = frozenset({"semaforo_rojo", "deadline_fatal", "deadline_added"})
# Un plazo vencido hace más de esto deja de aparecer en "Requiere atención".
STALE_DEADLINE_DAYS = 30

AlertCategory = Literal["actionable", "activity", "all"]
AlertGroupBy = Literal["case_day"]


def _relabel_plazo(message: Optional[str], case: Optional[Case]) -> Optional[str]:
    """(A) Al mostrar: si el plazo de la causa ya venció, 'Próximo plazo' → 'Plazo
    vencido' en el texto de la alerta (las alertas viejas guardan el texto congelado)."""
    if not message or case is None or case.next_deadline_at is None:
        return message
    from app.services.deadline_engine import _today_chile

    nd = case.next_deadline_at
    fecha = nd.date() if hasattr(nd, "date") else nd
    if fecha < _today_chile() and "Próximo plazo" in message:
        return message.replace("Próximo plazo", "Plazo vencido")
    return message


def _apply_category_filter(query, category: AlertCategory):
    """Narrow an Alert query to the given category. `all` is a no-op."""
    if category == "actionable":
        return query.filter(Alert.type.in_(ACTIONABLE_ALERT_TYPES))
    if category == "activity":
        return query.filter(Alert.type.notin_(ACTIONABLE_ALERT_TYPES))
    return query


def _scoped_alert_query(db: Session, lawyer_id: int, category: AlertCategory):
    """The caller's own alerts within ``category`` (incl. the stale-plazo rule)."""
    base_query = _apply_category_filter(
        db.query(Alert).filter(Alert.lawyer_id == lawyer_id), category
    )

    # (B) En "Requiere atención": excluir alertas de plazo cuya causa tiene un plazo
    # vencido hace más de STALE_DEADLINE_DAYS — ruido histórico, no acción del día.
    if category == "actionable":
        from app.services.deadline_engine import _today_chile

        cutoff = _today_chile() - timedelta(days=STALE_DEADLINE_DAYS)
        stale_cases = (
            db.query(Case.id).filter(Case.next_deadline_at < cutoff).subquery()
        )
        base_query = base_query.filter(
            ~(
                Alert.type.in_(DEADLINE_ALERT_TYPES)
                & Alert.case_id.in_(db.query(stale_cases.c.id))
            )
        )
    return base_query


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


class AlertGroupItem(BaseModel):
    """One (case, Chile day) group of alerts (``group_by=case_day``)."""

    group_key: str
    case_id: Optional[int] = None
    rol: Optional[str] = None
    caratula: Optional[str] = None
    day: str
    count: int
    unread_count: int
    latest_created_at: datetime
    latest_title: Optional[str] = None
    latest_message: Optional[str] = None
    types: List[str]
    alert_ids: List[int]


class AlertGroupListResponse(BaseModel):
    items: List[AlertGroupItem]
    total: int
    page: int
    per_page: int
    pages: int
    unread_count: int


class MarkAllReadResponse(BaseModel):
    marked: int


class ReadGroupRequest(BaseModel):
    group_key: str
    category: AlertCategory = "activity"


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
        message=_relabel_plazo(alert.message, case),
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


def _grouped_response(db, base_query, page, per_page, unread_only):
    rows, total, unread_groups = alert_groups.list_groups(
        db, base_query, page=page, per_page=per_page, unread_only=unread_only
    )
    case_ids = {r["case_id"] for r in rows if r["case_id"] is not None}
    cases_by_id = {}
    if case_ids:
        cases_by_id = {
            c.id: c for c in db.query(Case).filter(Case.id.in_(case_ids)).all()
        }
    items = []
    for r in rows:
        case = cases_by_id.get(r["case_id"])
        items.append(
            AlertGroupItem(
                group_key=alert_groups.make_group_key(r["case_id"], r["type"], r["day"]),
                case_id=r["case_id"],
                rol=case.rol if case else None,
                caratula=f"{case.plaintiff or ''}/{case.defendant or ''}" if case else None,
                day=r["day"],
                count=r["count"],
                unread_count=r["unread_count"],
                latest_created_at=r["latest_created_at"],
                latest_title=r["latest_title"],
                latest_message=_relabel_plazo(r["latest_message"], case),
                types=r["types"],
                alert_ids=r["alert_ids"],
            )
        )
    pages = (total + per_page - 1) // per_page if total > 0 else 0
    payload = AlertGroupListResponse(
        items=items, total=total, page=page, per_page=per_page,
        pages=pages, unread_count=unread_groups,
    )
    return JSONResponse(content=payload.model_dump(mode="json"))


@router.get("", response_model=AlertListResponse)
async def list_alerts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    category: AlertCategory = Query("actionable"),
    group_by: Optional[AlertGroupBy] = Query(None),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List the current lawyer's own alerts, most-recent-first, paginated.

    ``category`` splits the feed into ``actionable`` (semaforo_rojo,
    deadline_fatal/added/audit, credential_change, status_change — the few
    alerts that need a lawyer's attention), ``activity`` (everything else:
    new_movement, new_notificacion, new_exhorto, new_escrito — high-volume
    informational noise), or ``all`` (no type filter, current behavior).
    Defaults to ``actionable`` so the default feed and unread badge stay
    focused.

    ``unread_count`` reflects the lawyer's unread alerts WITHIN the
    requested ``category``, regardless of the ``unread_only`` filter or the
    current page.

    With ``group_by=case_day`` the items are groups keyed by (case, Chile
    calendar day) — see ``AlertGroupListResponse``; ``total`` and
    ``unread_count`` then count groups, and pagination applies to groups.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    base_query = _scoped_alert_query(db, lawyer_id, category)

    if group_by == "case_day":
        return _grouped_response(db, base_query, page, per_page, unread_only)

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
            c.id: c
            for c in db.query(Case)
            .options(joinedload(Case.court))
            .filter(Case.id.in_(case_ids))
            .all()
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
    category: AlertCategory = Query("all"),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Mark the caller's unread alerts within ``category`` as read.

    Defaults to ``all`` (preserves the previous "mark everything" behavior).
    Returns the count marked.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    now = datetime.utcnow()
    query = _apply_category_filter(
        db.query(Alert).filter(Alert.lawyer_id == lawyer_id, Alert.read.is_(False)),
        category,
    )
    marked = query.update({Alert.read: True, Alert.read_at: now}, synchronize_session=False)
    db.commit()

    return MarkAllReadResponse(marked=marked)


@router.post("/read-group", response_model=MarkAllReadResponse)
async def mark_group_read(
    body: ReadGroupRequest,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Mark the caller's unread alerts of one ``group_by=case_day`` group as read.

    ``group_key`` is the key returned by the grouped list; ``category``
    (default ``activity``) must match the tab the group was listed in so
    alerts of the other category on the same case/day are left untouched.
    Only the caller's own alerts are touched. Returns the count marked.
    """
    try:
        case_id, type_, day = alert_groups.parse_group_key(body.group_key)
    except alert_groups.InvalidGroupKey:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid group_key",
        )
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    ids = [
        row[0]
        for row in _scoped_alert_query(db, lawyer_id, body.category)
        .filter(
            Alert.read.is_(False),
            alert_groups.group_filter(db, case_id, type_, day),
        )
        .with_entities(Alert.id)
        .all()
    ]
    marked = 0
    if ids:
        marked = (
            db.query(Alert)
            .filter(Alert.lawyer_id == lawyer_id, Alert.id.in_(ids))
            .update(
                {Alert.read: True, Alert.read_at: datetime.utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
    return MarkAllReadResponse(marked=marked)
