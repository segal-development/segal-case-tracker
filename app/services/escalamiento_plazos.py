"""Fase 0 de la auditoría: plazo fatal vencido sin escrito.

* ``fatal_vencido_reddening`` / ``motivo_*``: pure rules the semáforo engine uses (S2/S3).
* ``escalate_fatal_vencidos``: ONE in-app alert per (deadline, recipient) for the case's
  lawyer(s) and every active admin (S4). Idempotent on
  ``(Alert.entity_type="plazo_fatal_escalado", Alert.entity_id=<case_deadlines.id>,
  Alert.type="plazo_fatal_vencido_escalado", Alert.lawyer_id)``. In-app only: the
  e-mail jobs select on other types/prefixes, so nothing here is mailed or sent to Telegram.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deadlines_config import DEADLINE_LABELS, FATAL_DEADLINE_VALUES
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer

logger = logging.getLogger(__name__)

#: A fatal plazo lost more than this many days ago no longer reddens the case (still listed).
REDDEN_WINDOW_DAYS = 60

MOTIVO_ABANDONO = "Abandono disponible: presentar el escrito"
ALERT_TYPE = "plazo_fatal_vencido_escalado"
ALERT_ENTITY_TYPE = "plazo_fatal_escalado"


def motivo_fatal_vencido(deadline_type: str, due: date) -> str:
    tipo = DEADLINE_LABELS.get(deadline_type, deadline_type)
    return f"Plazo fatal vencido sin escrito: {tipo} venció el {due.strftime('%d-%m-%Y')}"


def is_unreviewed_fatal_vencido(row: CaseDeadline, today: date) -> bool:
    """Fatal, ``no_cumplido``, already past due and not touched by a human."""
    return (
        row.status == "no_cumplido"
        and row.deadline_type in FATAL_DEADLINE_VALUES
        and row.marked_by is None
        and row.marked_at is None
        and row.due_date < today
    )


def reddens(row: CaseDeadline, today: date) -> bool:
    return row.due_date >= today - timedelta(days=REDDEN_WINDOW_DAYS)


def _owners(db: Session, case: Case) -> list[Lawyer]:
    """The case's lawyer(s) of record (one resolution per case), owner as fallback."""
    from app.services.lawyer_roster import resolve_case_alert_recipients

    owners = list(resolve_case_alert_recipients(db, case))
    if owners:
        return owners
    fallback = db.query(Lawyer).filter(Lawyer.id == case.effective_lawyer_id).first()
    return [fallback] if fallback is not None else []


def _recipient_ids(db: Session, owners: list[Lawyer]) -> list[int]:
    ids = [o.id for o in owners]
    admins = db.query(Lawyer.id).filter(Lawyer.role == "admin", Lawyer.is_active.is_(True)).all()
    for (admin_id,) in admins:
        if admin_id not in ids:
            ids.append(admin_id)
    return ids


def escalate_fatal_vencidos(
    db: Session, case: Case, rows: Iterable[CaseDeadline], today: date
) -> int:
    """Create the missing escalation alerts for ``rows``; returns how many. Flush-only."""
    # Only inside the S3 window (older ones are listed, never alerted); gris/archived exempt.
    rows = [r for r in rows if is_unreviewed_fatal_vencido(r, today) and reddens(r, today)]
    if not rows or case.status == "archived" or case.semaforo == "gris":
        return 0
    owners = _owners(db, case)
    recipients = _recipient_ids(db, owners)
    if not recipients:
        return 0
    existing = {
        (eid, lid)
        for eid, lid in db.query(Alert.entity_id, Alert.lawyer_id).filter(
            Alert.entity_type == ALERT_ENTITY_TYPE,
            Alert.type == ALERT_TYPE,
            Alert.entity_id.in_([r.id for r in rows]),
        )
    }
    owner_ids = [o.id for o in owners]
    names = dict(db.query(Lawyer.id, Lawyer.name).filter(Lawyer.id.in_(owner_ids)).all()) if owner_ids else {}
    abogado = ", ".join(names[i] for i in owner_ids if i in names) or "sin abogado asignado"
    created = 0
    for row in rows:
        title = f"Plazo fatal vencido sin escrito en {case.rol}"
        tipo = DEADLINE_LABELS.get(row.deadline_type, row.deadline_type)
        message = (
            f"{tipo} venció el {row.due_date.strftime('%d-%m-%Y')} sin escrito presentado. "
            f"Abogado: {abogado}."
        )
        for lawyer_id in recipients:
            if (row.id, lawyer_id) in existing:
                continue
            try:
                # Savepoint per insert: a concurrent worker that won the race trips the
                # partial unique index; that alert already exists, so just move on.
                with db.begin_nested():
                    db.add(Alert(
                        lawyer_id=lawyer_id, case_id=case.id, type=ALERT_TYPE,
                        entity_type=ALERT_ENTITY_TYPE, entity_id=row.id,
                        title=title, message=message, created_at=datetime.utcnow(),
                    ))
            except IntegrityError:
                continue
            existing.add((row.id, lawyer_id))
            created += 1
    return created
