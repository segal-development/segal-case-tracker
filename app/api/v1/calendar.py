"""Calendar endpoint — slice 2a/2b of req #5/#11 (calendarización).

Agenda of upcoming/overdue DEADLINES and REVIEW dates across the caller's
scoped cases, sorted by date ascending. Reuses data already denormalized on
``Case`` at the end of every sync (``DeadlineEngine.recompute_case`` /
``DecisionEngine``): ``next_deadline_at``/``next_deadline_fatal`` and
``next_review_at``/``recommended_action_code``. This endpoint performs no
new computation — it is a read-only fan-out + windowing view over columns
the engine already writes. Deadline items additionally resolve a real
``label`` by looking up the backing ``CaseDeadline`` row (same resolution
``GET /cases/{id}/deadlines`` already performs via ``DEADLINE_LABELS``).
"""

from datetime import date, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import (
    get_db,
    get_current_lawyer,
    resolve_case_scope,
    apply_case_scope,
)
from app.core.decision_rules import resolve_rule
from app.core.deadlines_config import DEADLINE_LABELS
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer
from app.services.deadline_engine import _today_chile

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class CalendarItemResponse(BaseModel):
    """One agenda entry: either a procedural deadline or a DecisionEngine
    review date. ``fatal``/``label`` are only meaningful for
    ``kind="deadline"``; ``recommended_action``/``urgency`` only for
    ``kind="review"``."""

    date: date
    kind: Literal["deadline", "review"]
    case_id: int
    rol: str
    caratulado: str
    court_name: Optional[str] = None
    semaforo: Optional[str] = None
    fatal: Optional[bool] = None
    label: Optional[str] = None
    recommended_action: Optional[str] = None
    urgency: Optional[str] = None
    overdue: bool

    model_config = ConfigDict(from_attributes=True)


class CalendarSummaryResponse(BaseModel):
    total: int
    overdue_count: int
    fatal_count: int

    model_config = ConfigDict(from_attributes=True)


class CalendarResponse(BaseModel):
    items: List[CalendarItemResponse]
    summary: CalendarSummaryResponse

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Windowing rules
# ---------------------------------------------------------------------------


def _is_still_active(case: Case) -> bool:
    """Whether an overdue item on this case should still count as
    actionable. Simple rule: semaforo not in {verde, gris} — those two
    mean nothing urgent is pending (cleared / not applicable)."""
    return case.semaforo not in ("verde", "gris")


def _resolve_window(
    item_date: date,
    today: date,
    horizon_end: date,
    include_overdue: bool,
    case: Case,
) -> tuple[bool, bool]:
    """Return ``(include, overdue)`` for a single candidate date against the
    ``[today, horizon_end]`` window.

    - Inside the window (inclusive both ends): always included, not overdue.
    - Before ``today`` (past-due): included only when ``include_overdue`` is
      true AND the case is still active (``_is_still_active``); overdue=True.
    - Beyond ``horizon_end``: excluded.
    """
    if today <= item_date <= horizon_end:
        return True, False
    if item_date < today:
        if include_overdue and _is_still_active(case):
            return True, True
    return False, False


def _caratulado(case: Case) -> str:
    return f"{case.plaintiff or ''}/{case.defendant or ''}"


def _deadline_labels_by_case(db: Session, cases: List[Case]) -> dict:
    """Map ``{case_id: label}`` for the cases' next active deadline, in ONE query.

    Reuses the SAME resolution ``GET /cases/{id}/deadlines`` already performs
    (look up the CaseDeadline row and map its ``deadline_type`` through the
    shared ``DEADLINE_LABELS`` catalog) instead of reinventing it here — this
    endpoint stays a read-only fan-out over engine-computed state.

    A case with no matching active row is simply absent from the map, so the
    caller reads None (e.g. a case whose ``next_deadline_at`` was set without a
    backing CaseDeadline row — should not happen via the real engine pipeline,
    but kept safe for callers that write the denormalized column directly,
    such as tests/fixtures).

    Batched on purpose: the sidebar calls this endpoint on every page load, and
    resolving the label per case cost one round trip over the Cloud SQL proxy
    for each case carrying a deadline.
    """
    wanted = {c.id: c.next_deadline_at for c in cases if c.next_deadline_at is not None}
    if not wanted:
        return {}

    rows = (
        db.query(CaseDeadline)
        .filter(
            CaseDeadline.case_id.in_(list(wanted)),
            CaseDeadline.status == "active",
        )
        .order_by(CaseDeadline.id.asc())
        .all()
    )

    labels: dict = {}
    for row in rows:
        # Same predicate the per-case query applied: the row must match the
        # case's own next_deadline_at, and the LOWEST id wins.
        if row.due_date != wanted.get(row.case_id) or row.case_id in labels:
            continue
        labels[row.case_id] = DEADLINE_LABELS.get(row.deadline_type, row.deadline_type)
    return labels


@router.get("", response_model=CalendarResponse)
async def get_calendar(
    days: int = Query(30, ge=1, description="Horizon in days from today (inclusive)"),
    include_overdue: bool = Query(
        True, description="Also include past-due items that are still active"
    ),
    abogado_rut: Optional[str] = Query(
        None,
        description=(
            "Narrow the calendar to cases where this RUT is a firm-side abogado. "
            "Lets an auditor/admin see one lawyer's personal agenda; mirrors /cases."
        ),
    ),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Agenda of upcoming/overdue deadlines and review dates.

    Scoped like the stats endpoints: a regular lawyer sees only cases where
    they are an abogado-of-record litigante; auditor/admin see every study
    case (``resolve_case_scope`` / ``apply_case_scope``). Archived cases are
    excluded. Each case contributes up to two items: a ``deadline`` item
    when ``next_deadline_at`` falls in the window, and a ``review`` item
    when ``next_review_at`` does — the review item's ``recommended_action``
    text/urgency is resolved from ``Case.recommended_action_code`` via
    ``app.core.decision_rules.resolve_rule`` (same resolution the
    ``/cases/{id}/deadlines`` endpoint uses).
    """
    scope = resolve_case_scope(db, current_lawyer)
    # Only cases that can actually produce an item. A causa with neither date
    # contributes nothing to the response, so hydrating it is pure waste: on the
    # real portfolio that is 11.959 of 14.645 rows discarded in Python, on an
    # endpoint the sidebar calls on every page load. The horizon is NOT filtered
    # here — an overdue item may be arbitrarily old — and it would buy almost
    # nothing anyway (2.686 -> 2.622 rows at 30 days).
    query = (
        db.query(Case)
        .options(joinedload(Case.court))
        .filter(
            Case.status != "archived",
            or_(Case.next_deadline_at.isnot(None), Case.next_review_at.isnot(None)),
        )
    )
    query = apply_case_scope(query, scope)

    # Optional per-abogado narrowing (mirrors /cases): an auditor/admin can pull
    # one lawyer's personal agenda by passing that lawyer's RUT. Intersect the
    # scoped set with the cases where abogado_rut is an abogado-of-record.
    if abogado_rut:
        from app.api.deps import _resolve_lawyer_id
        from app.services.lawyer_roster import case_ids_for_abogado

        account_lawyer = db.get(Lawyer, _resolve_lawyer_id(db, current_lawyer))
        if account_lawyer:
            allowed_ids = case_ids_for_abogado(db, account_lawyer.rut, abogado_rut)
            query = query.filter(Case.id.in_(list(allowed_ids)))

    cases = query.all()
    deadline_labels = _deadline_labels_by_case(db, cases)

    today = _today_chile()
    horizon_end = today + timedelta(days=days)

    items: List[CalendarItemResponse] = []
    for case in cases:
        court_name = case.court.name if case.court else None
        caratulado = _caratulado(case)

        if case.next_deadline_at is not None:
            include, overdue = _resolve_window(
                case.next_deadline_at, today, horizon_end, include_overdue, case
            )
            if include:
                items.append(
                    CalendarItemResponse(
                        date=case.next_deadline_at,
                        kind="deadline",
                        case_id=case.id,
                        rol=case.rol,
                        caratulado=caratulado,
                        court_name=court_name,
                        semaforo=case.semaforo,
                        fatal=bool(case.next_deadline_fatal),
                        label=deadline_labels.get(case.id),
                        overdue=overdue,
                    )
                )

        if case.next_review_at is not None:
            include, overdue = _resolve_window(
                case.next_review_at, today, horizon_end, include_overdue, case
            )
            if include:
                rule = resolve_rule(case.recommended_action_code)
                items.append(
                    CalendarItemResponse(
                        date=case.next_review_at,
                        kind="review",
                        case_id=case.id,
                        rol=case.rol,
                        caratulado=caratulado,
                        court_name=court_name,
                        semaforo=case.semaforo,
                        recommended_action=rule.action_text if rule else None,
                        urgency=rule.urgency.value if rule else None,
                        overdue=overdue,
                    )
                )

    items.sort(key=lambda i: (i.date, i.case_id, i.kind))

    summary = CalendarSummaryResponse(
        total=len(items),
        overdue_count=sum(1 for i in items if i.overdue),
        fatal_count=sum(1 for i in items if i.fatal),
    )

    return CalendarResponse(items=items, summary=summary)
