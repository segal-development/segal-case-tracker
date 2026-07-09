"""Timeline service — read-only case lifecycle aggregation (req #8, slice 1).

Merges timestamped rows from Movement, Document, CaseDeadline, Alert,
CaseEscrito, CaseNotificacion, and CaseExhorto into a single per-case
TimelineEvent feed, sorted most-recent-first and paginated.

Scope: this is INBOUND / OPERATIONAL traceability only — movements detected,
document ingestion/storage/failure, deadline computation/manual audit,
alerts, and detected escritos/notificaciones/exhortos. OUTBOUND traceability
(drafted -> signed -> filed generated documents, reqs #3/#4) is out of scope
until those data models exist.

Deliberately NOT backed by a new event/audit table: every source below
already carries a correct, usable timestamp, so this module reads and merges
them at request time instead of resurrecting the dead, unused
``app.models.audit_log.AuditLog`` table (see sdd/case-lifecycle-
traceability/explore for the full rationale).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Iterable, List, Literal, Optional

from pydantic import BaseModel

from app.core.deadlines_config import DEADLINE_LABELS
from app.models.alert import Alert
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_notificacion import CaseNotificacion
from app.models.document import Document
from app.models.movement import Movement

TimelineKind = Literal[
    "movimiento",
    "documento",
    "plazo",
    "alerta",
    "escrito",
    "notificacion",
    "exhorto",
]


class TimelineEvent(BaseModel):
    """One unified, case-scoped lifecycle event."""

    date: datetime
    kind: TimelineKind
    title: str
    description: Optional[str] = None
    ref_id: Optional[int] = None
    status: Optional[str] = None


class CaseTimelinePage(BaseModel):
    """Paginated, merged timeline for a single case."""

    items: List[TimelineEvent]
    total: int
    page: int
    per_page: int
    pages: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_not_none(*values):
    """Return the first non-None value, in the given priority order."""
    for value in values:
        if value is not None:
            return value
    return None


def _as_datetime(value):
    """Normalize a date/datetime into a datetime for uniform sorting."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return value


# ---------------------------------------------------------------------------
# Per-source mappers — small, pure, independently testable
# ---------------------------------------------------------------------------


def map_movement(m: Movement) -> TimelineEvent:
    extra = " / ".join(part for part in (m.stage, m.procedure) if part)
    return TimelineEvent(
        date=m.movement_date,
        kind="movimiento",
        title=m.description,
        description=extra or None,
        ref_id=m.id,
        status=None,
    )


def map_document(d: Document) -> TimelineEvent:
    event_date = _first_not_none(d.stored_at, d.failed_at, d.downloaded_at)
    return TimelineEvent(
        date=event_date,
        kind="documento",
        title=d.doc_type or d.filename or "Documento",
        description=d.filename,
        ref_id=d.id,
        status=d.status,
    )


def map_deadline(dl: CaseDeadline) -> TimelineEvent:
    label = DEADLINE_LABELS.get(dl.deadline_type, dl.deadline_type)
    event_date = _as_datetime(
        _first_not_none(dl.triggered_at, dl.marked_at, dl.created_at)
    )
    return TimelineEvent(
        date=event_date,
        kind="plazo",
        title=f"{label} — {dl.status}",
        description=dl.legal_basis,
        ref_id=dl.id,
        status=dl.status,
    )


def map_alert(a: Alert) -> TimelineEvent:
    return TimelineEvent(
        date=a.created_at,
        kind="alerta",
        title=a.title,
        description=a.message,
        ref_id=a.id,
        status=None,
    )


def map_escrito(e: CaseEscrito) -> TimelineEvent:
    event_date = _first_not_none(e.fecha_ingreso, e.created_at)
    return TimelineEvent(
        date=event_date,
        kind="escrito",
        title=e.tipo_escrito,
        description=e.solicitante,
        ref_id=e.id,
        status=None,
    )


def map_notificacion(n: CaseNotificacion) -> TimelineEvent:
    event_date = _first_not_none(n.fecha_tramite, n.created_at)
    return TimelineEvent(
        date=event_date,
        kind="notificacion",
        title=n.tipo_notif,
        description=n.tramite,
        ref_id=n.id,
        status=n.estado_notif,
    )


def map_exhorto(ex: CaseExhorto) -> TimelineEvent:
    event_date = _first_not_none(ex.fecha_ingreso, ex.fecha_ordena, ex.created_at)
    return TimelineEvent(
        date=event_date,
        kind="exhorto",
        title=ex.tipo_exhorto,
        description=f"{ex.rol_origen} → {ex.rol_destino}",
        ref_id=ex.id,
        status=ex.estado,
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def build_case_timeline(
    *,
    movements: Iterable[Movement] = (),
    documents: Iterable[Document] = (),
    deadlines: Iterable[CaseDeadline] = (),
    alerts: Iterable[Alert] = (),
    escritos: Iterable[CaseEscrito] = (),
    notificaciones: Iterable[CaseNotificacion] = (),
    exhortos: Iterable[CaseExhorto] = (),
    page: int = 1,
    per_page: int = 30,
) -> CaseTimelinePage:
    """Merge every source into one TimelineEvent feed, sorted most-recent-first.

    Read-only: callers are responsible for querying each source scoped to the
    target case_id and passing the resulting rows in. This function performs
    no DB I/O of its own.
    """
    events: List[TimelineEvent] = []
    events.extend(map_movement(m) for m in movements)
    events.extend(map_document(d) for d in documents)
    events.extend(map_deadline(dl) for dl in deadlines)
    events.extend(map_alert(a) for a in alerts)
    events.extend(map_escrito(e) for e in escritos)
    events.extend(map_notificacion(n) for n in notificaciones)
    events.extend(map_exhorto(ex) for ex in exhortos)

    events.sort(key=lambda e: e.date, reverse=True)

    total = len(events)
    pages = (total + per_page - 1) // per_page if total > 0 else 0
    start = (page - 1) * per_page
    end = start + per_page

    return CaseTimelinePage(
        items=events[start:end],
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )
