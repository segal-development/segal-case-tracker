"""Calendar endpoint — slice 2a of req #5/#11 (calendarización).

Agenda of upcoming/overdue DEADLINES and REVIEW dates across the caller's
scoped cases, sorted by date ascending. Reuses data already denormalized on
``Case`` at the end of every sync (``DeadlineEngine.recompute_case`` /
``DecisionEngine``): ``next_deadline_at``/``next_deadline_fatal`` and
``next_review_at``/``recommended_action_code``. This endpoint performs no
new computation — it is a read-only fan-out + windowing view over columns
the engine already writes.
"""

from datetime import date, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_current_lawyer,
    resolve_case_scope,
    apply_case_scope,
)
from app.core.decision_rules import resolve_rule
from app.models.case import Case
from app.services.deadline_engine import _today_chile

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class CalendarItemResponse(BaseModel):
    """One agenda entry: either a procedural deadline or a DecisionEngine
    review date. ``fatal`` is only meaningful for ``kind="deadline"``;
    ``recommended_action``/``urgency`` only for ``kind="review"``."""

    date: date
    kind: Literal["deadline", "review"]
    case_id: int
    rol: str
    caratulado: str
    court_name: Optional[str] = None
    semaforo: Optional[str] = None
    fatal: Optional[bool] = None
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


@router.get("", response_model=CalendarResponse)
async def get_calendar(
    days: int = Query(30, ge=1, description="Horizon in days from today (inclusive)"),
    include_overdue: bool = Query(
        True, description="Also include past-due items that are still active"
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
    query = db.query(Case).filter(Case.status != "archived")
    query = apply_case_scope(query, scope)
    cases = query.all()

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
