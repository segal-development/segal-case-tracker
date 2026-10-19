"""Read-only data for the home-screen fatal-plazo panel.

Two groups, both restricted to the firm's OWN fatal plazos:

* ``por_vencer``: live plazos. The level comes ONLY from
  ``deadline_semaforo.assess``; a plazo whose assessment has no level (or no
  semaforo at all, e.g. ``apelacion_5d``) is left out instead of being painted
  with a rule invented here.
* ``vencidos``: plazos that expired with the firm not having acted, selected by
  ``semaforo_alerts.expired_unfiled_deadlines`` (same verdict, window and
  case filter as the expired-notice alerts). They carry no level: their colour
  is not a semaforo reading, the screen shows them as expired.

Nothing is written: no alerts, no emails. The creation paths stay in
``semaforo_alerts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Collection, List, Optional

from sqlalchemy.orm import Session

from app.core.deadlines_config import DEADLINE_LABELS, FATAL_DEADLINE_VALUES
from app.core.semaforo_config import URGENCY, Level
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer
from app.services.business_days import count_business_days_remaining
from app.services.deadline_semaforo import Assessment, assess
from app.services.semaforo_alerts import (
    _confirmed_term,
    _is_ours,
    expired_unfiled_deadlines,
)


@dataclass
class ReadingRow:
    term_days: int
    due_date: date
    #: None once that reading's last day has gone by.
    level: Optional[str]


@dataclass
class PanelRow:
    deadline_id: int
    case_id: int
    rol: str
    caratula: str
    tribunal: Optional[str]
    abogado_id: Optional[int]
    abogado_nombre: Optional[str]
    deadline_type: str
    label: str
    due_date: date
    dias_habiles_remaining: int
    #: ``assess`` level for ``por_vencer`` rows; always None for ``vencidos``.
    level: Optional[str]
    #: False when the term is one of several possible ones (excepciones).
    term_confirmed: Optional[bool]
    readings: List[ReadingRow] = field(default_factory=list)


@dataclass
class Panel:
    por_vencer: List[PanelRow]
    vencidos: List[PanelRow]


def _readings(a: Optional[Assessment]) -> List[ReadingRow]:
    if a is None:
        return []
    return [
        ReadingRow(r.term_days, r.due_date, r.level.value if r.level else None)
        for r in a.readings
    ]


def _row(
    db: Session,
    row: CaseDeadline,
    case: Case,
    today: date,
    a: Optional[Assessment],
    level: Optional[str],
    due: date,
) -> PanelRow:
    lawyer_id = case.effective_lawyer_id
    lawyer = db.get(Lawyer, lawyer_id) if lawyer_id is not None else None
    return PanelRow(
        deadline_id=row.id,
        case_id=case.id,
        rol=case.rol or "",
        caratula=f"{case.plaintiff or ''}/{case.defendant or ''}",
        tribunal=case.court.name if case.court else None,
        abogado_id=lawyer_id,
        abogado_nombre=lawyer.name if lawyer else None,
        deadline_type=row.deadline_type,
        label=DEADLINE_LABELS.get(row.deadline_type, row.deadline_type),
        due_date=due,
        dias_habiles_remaining=count_business_days_remaining(due, today),
        level=level,
        term_confirmed=a.confirmed if a else None,
        readings=_readings(a),
    )


def _por_vencer(db: Session, today: date, case_ids: Optional[Collection[int]]) -> List[PanelRow]:
    query = (
        db.query(CaseDeadline, Case)
        .join(Case, Case.id == CaseDeadline.case_id)
        .filter(
            CaseDeadline.deadline_type.in_(FATAL_DEADLINE_VALUES),
            CaseDeadline.status == "active",
            CaseDeadline.due_date >= today,
            Case.status == "active",
        )
    )
    if case_ids is not None:
        query = query.filter(Case.id.in_(case_ids))

    rows: List[PanelRow] = []
    for dl, case in query.order_by(CaseDeadline.id).all():
        a = assess(dl.deadline_type, dl.triggered_at, today, _confirmed_term(dl))
        if a is None or a.level is None:
            continue
        if not _is_ours(db, case, dl, today):
            continue
        due = a.earliest_live_due or dl.due_date
        rows.append(_row(db, dl, case, today, a, a.level.value, due))
    rows.sort(key=lambda r: (-URGENCY.index(Level(r.level)), r.due_date, r.deadline_id))
    return rows


def _vencidos(db: Session, today: date, case_ids: Optional[Collection[int]]) -> List[PanelRow]:
    rows: List[PanelRow] = []
    for dl in expired_unfiled_deadlines(db, today):
        if case_ids is not None and dl.case_id not in case_ids:
            continue
        case = db.get(Case, dl.case_id)
        if case is None or not _is_ours(db, case, dl, today):
            continue
        a = assess(dl.deadline_type, dl.triggered_at, today, _confirmed_term(dl))
        rows.append(_row(db, dl, case, today, a, None, dl.due_date))
    rows.sort(key=lambda r: (r.due_date, r.deadline_id), reverse=True)
    return rows


def build_panel(
    db: Session, today: date, case_ids: Optional[Collection[int]] = None
) -> Panel:
    """The panel as of ``today``. ``case_ids`` None = the whole firm."""
    return Panel(
        por_vencer=_por_vencer(db, today, case_ids),
        vencidos=_vencidos(db, today, case_ids),
    )
