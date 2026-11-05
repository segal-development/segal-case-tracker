"""The plazo semaforo run: raise the alert each live plazo is due, and send it.

Level changes are driven by the CALENDAR, not by a sync event, so this is a
periodic, idempotent scan (``run_semaforo_alerts``) rather than a hook on a
transition. It is safe to call as often as you like: an alert is created once
per (plazo, slot, level, recipient) and an unsent one is retried until its term
is over.

    verde     -> the case's lawyer(s)                      grouped, 1 email/day
    amarillo  -> + SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS      immediate, 1 per plazo
    rojo      -> + SEMAFORO_ROJO_EXTRA_LAWYER_IDS          immediate, 1 per plazo

Dedupe key: ``Alert.entity_type = "plazo:<deadline_type>:<slot>"``,
``entity_id = case_deadlines.id``, ``type = plazo_<level>``, ``lawyer_id``. The
slot (see ``deadline_semaforo``) is what lets an UNCONFIRMED excepciones plazo
legitimately alert amarillo twice, once per possible reading.

NEVER breaks the sync cycle: ``run_semaforo_alerts`` catches everything.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deadlines_config import (
    FATAL_DEADLINE_VALUES,
    OPTIONAL_ACTIONABLE_VALUES,
    PARALLEL_DEADLINE_VALUES,
)
from app.core.semaforo_config import (
    EXPIRED_ALERT_TYPE,
    ALERT_TYPE_BY_LEVEL,
    LEVEL_BY_ALERT_TYPE,
    SEMAFORO_ALERT_TYPES,
    URGENCY,
    Level,
    parse_lawyer_ids,
)
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer
from app.models.webhook import Webhook
from app.services.deadline_engine import (
    _firm_side,
    _today_chile,
    medida_prejudicial_clause,
)
from app.services.deadline_verdict import Verdict
from app.services.deadline_semaforo import (
    EXCEPCIONES,
    PODER,
    Assessment,
    assess,
    confirmed_excepciones_term,
)
from app.services.notification_service import NotificationService
from app.services.plazo_copy import build_alert_text, build_vencido_text

logger = logging.getLogger(__name__)

_TYPES = (EXCEPCIONES, PODER)
_ENTITY_PREFIX = "plazo:"

#: Verdicts that say the firm did NOT act. A NULL verdict is "unknown", not
#: "did not act", and is deliberately absent: warning about a doubt is noise.
#: Fatal plazos this notice covers: the config's fatal set, minus the parallel
#: ones, minus the OPTIONAL ones.
#:
#: ``acreditar_poder_3d`` (parallel) is fatal but has its own detection and its
#: own alert ("ratificar_firma"), and its ownership is not provable from the row.
#:
#: ``apelacion_5d`` (optional) is excluded because appealing is a RIGHT the firm
#: exercises case by case, not an obligation: "no todo conviene apelarlo (...) si
#: eventualmente vemos ganable se apela" (Carla Lavín, 2026-10-20). A window that
#: closes unused is a decision, not a miss, so "VENCIDO, no presentaste" would
#: report the firm's own strategy as a failure.
#:
#: Do NOT narrow this back to ``- PARALLEL_DEADLINE_VALUES``. Apelación currently
#: emits nothing only by accident -- ``deadline_verdict.DETECTORS`` has no
#: apelación detector, so its verdict stays NULL and the query below filters it
#: out. Adding that detector is a natural future change (the NULL rows look like
#: a bug); this subtraction is what keeps it from mailing hundreds of false
#: VENCIDO notices the day it lands.
_EXPIRED_NOTICE_TYPES = (
    FATAL_DEADLINE_VALUES - PARALLEL_DEADLINE_VALUES - OPTIONAL_ACTIONABLE_VALUES
)

_NOT_ACTED_VERDICTS = (Verdict.NO_CUMPLIDO.value, Verdict.FUERA_DE_PLAZO.value)


def entity_type_for(assessment: Assessment) -> str:
    return f"{_ENTITY_PREFIX}{assessment.deadline_type}:{assessment.slot}"


def _extra_lawyers(db: Session, level: Level) -> List[Lawyer]:
    """Escalation recipients for ``level`` from settings, in configured order.

    A configured id that does not resolve to an ACTIVE lawyer is logged and
    skipped; everybody else is still notified.
    """
    if level is Level.AMARILLO:
        name, raw = "SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS", settings.SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS
    elif level is Level.ROJO:
        name, raw = "SEMAFORO_ROJO_EXTRA_LAWYER_IDS", settings.SEMAFORO_ROJO_EXTRA_LAWYER_IDS
    else:
        return []
    out: List[Lawyer] = []
    for lawyer_id in parse_lawyer_ids(raw, setting=name):
        lawyer = db.get(Lawyer, lawyer_id)
        if lawyer is None or not lawyer.is_active:
            logger.warning(
                "%s: lawyer id %s is not an active lawyer; skipped (plazo %s alerts)",
                name, lawyer_id, level.value,
            )
            continue
        out.append(lawyer)
    return out


def _case_lawyers(db: Session, case: Case) -> List[Lawyer]:
    """The case's lawyer(s), same resolution as every other deadline alert."""
    from app.services.lawyer_roster import resolve_case_alert_recipients

    recipients = resolve_case_alert_recipients(db, case)
    if recipients:
        return list(recipients)
    fallback = db.get(Lawyer, case.effective_lawyer_id)
    return [fallback] if fallback else []


def _recipients(db: Session, case: Case, level: Level) -> List[Lawyer]:
    seen: set = set()
    out: List[Lawyer] = []
    for lawyer in [*_case_lawyers(db, case), *_extra_lawyers(db, level)]:
        if lawyer.id in seen:
            continue
        seen.add(lawyer.id)
        out.append(lawyer)
    return out


def _is_ours(db: Session, case: Case, row: CaseDeadline, today: date) -> bool:
    """Is this plazo the firm's own obligation? A window we do not own must not
    raise an alarm (it would tell a lawyer to chase somebody else's deadline)."""
    if row.deadline_type == PODER:
        from app.services.sync_service import poder_obligation_is_ours

        return poder_obligation_is_ours(db, case, row, today) is not None
    return _firm_side(db, case) != "demandante"


def _within_cutoff(row: Any, today: date) -> bool:
    """Is this plazo recent enough to alert on? Measured on ``triggered_at``.

    ``SEMAFORO_MAX_TRIGGER_AGE_DAYS`` <= 0 disables the cutoff. A plazo with no
    ``triggered_at`` is NOT recent: we cannot say when it was born, and a wrong
    alarm costs more than a missing one (fail towards silence). The check
    precedes the pure assessment, which cannot take a NULL date.
    """
    max_age = settings.SEMAFORO_MAX_TRIGGER_AGE_DAYS
    if max_age <= 0:
        return True
    triggered_at = getattr(row, "triggered_at", None)
    if triggered_at is None:
        return False
    return triggered_at >= today - timedelta(days=max_age)


def _existing_keys(db: Session, row_ids: List[int]) -> set:
    if not row_ids:
        return set()
    rows = (
        db.query(Alert.entity_type, Alert.entity_id, Alert.type, Alert.lawyer_id)
        .filter(
            Alert.entity_type.like(f"{_ENTITY_PREFIX}%"),
            Alert.entity_id.in_(row_ids),
        )
        .all()
    )
    return {tuple(r) for r in rows}


def _candidates(db: Session, today: date) -> List[CaseDeadline]:
    return (
        db.query(CaseDeadline)
        .join(Case, Case.id == CaseDeadline.case_id)
        .filter(
            CaseDeadline.deadline_type.in_(_TYPES),
            CaseDeadline.status == "active",
            CaseDeadline.is_manual == False,  # noqa: E712
            CaseDeadline.due_date >= today,
            Case.status != "archived",
            ~medida_prejudicial_clause(),
        )
        .order_by(CaseDeadline.id)
        .all()
    )


def expired_unfiled_deadlines(db: Session, today: date) -> List[CaseDeadline]:
    """FATAL plazos that expired with the firm NOT having acted. Read-only.

    Selected on the verdict and the date, never on the row ``status`` (the two
    are independent: a ``superseded`` row can carry ``no_cumplido``). Only
    ``due_date`` within ``SEMAFORO_EXPIRED_WINDOW_DAYS`` of ``today`` (<= 0 =
    no window) on a case that is still ``active``.

    One exception to "never on the status": a plazo an AUDITOR signed as
    ``cumplido`` (``marked_by`` set) is met, whatever the engine's verdict still
    says. The verdict is evidence the engine computed; the auditor's decision
    overrides it, and the plazo must not keep showing as lost or raise alerts.
    """
    query = (
        db.query(CaseDeadline)
        .join(Case, Case.id == CaseDeadline.case_id)
        .filter(
            CaseDeadline.deadline_type.in_(_EXPIRED_NOTICE_TYPES),
            CaseDeadline.due_date < today,
            CaseDeadline.verdict.in_(_NOT_ACTED_VERDICTS),
            ~and_(CaseDeadline.status == "cumplido", CaseDeadline.marked_by.isnot(None)),
            Case.status == "active",
            ~medida_prejudicial_clause(),
        )
    )
    window = settings.SEMAFORO_EXPIRED_WINDOW_DAYS
    if window > 0:
        query = query.filter(CaseDeadline.due_date >= today - timedelta(days=window))
    return query.order_by(CaseDeadline.id).all()


def _create_expired_alerts(
    db: Session, row: CaseDeadline, today: date, existing: set
) -> int:
    """Create (and commit) the one-time expired-and-unfiled alerts of ``row``.

    Returns how many were created. Dedupe reuses ``existing`` (see
    ``_existing_keys``): the key is (entity_type, plazo id, type, lawyer), so a
    plazo is announced once, ever, per recipient.
    """
    case = db.get(Case, row.case_id)
    if case is None:
        return 0
    entity_type = f"{_ENTITY_PREFIX}{row.deadline_type}:vencido"
    # Escalation recipients are the same people who are told of a rojo.
    recipients = _recipients(db, case, Level.ROJO)
    if all((entity_type, row.id, EXPIRED_ALERT_TYPE, r.id) in existing for r in recipients):
        return 0
    # Excepciones is only the firm's obligation when it defends the debtor.
    if row.deadline_type == EXCEPCIONES and not _is_ours(db, case, row, today):
        return 0

    tribunal = case.court.name if case.court else "tribunal no especificado"
    title, message = build_vencido_text(
        row.deadline_type, row.due_date, row.verdict, case.rol or "", tribunal,
        # No movement behind the verdict = the evidence is an escrito, whose
        # filing date is known (see ``DeadlineEngine._apply_verdict``).
        filed_on=row.verdict_acted_on if row.verdict_movement_id is None else None,
    )
    created = 0
    for recipient in recipients:
        key = (entity_type, row.id, EXPIRED_ALERT_TYPE, recipient.id)
        if key in existing:
            continue
        db.add(Alert(
            lawyer_id=recipient.id,
            case_id=case.id,
            type=EXPIRED_ALERT_TYPE,
            entity_type=entity_type,
            entity_id=row.id,
            title=title,
            message=message,
        ))
        existing.add(key)
        created += 1
    db.commit()
    return created


def _run_expired_notice(db: Session, today: date, summary: Dict[str, Any]) -> None:
    """Raise the expired-and-unfiled alerts. NEVER raises: a failure here is
    logged and counted, and must not stop the live-term alerts or the dispatch."""
    try:
        rows = expired_unfiled_deadlines(db, today)
        existing = _existing_keys(db, [r.id for r in rows])
        for row in rows:
            try:
                summary["expired_created"] += _create_expired_alerts(db, row, today, existing)
            except Exception:
                summary["errors"] += 1
                logger.exception(
                    "semaforo: could not raise the expired notice for case_deadline %s; skipped",
                    getattr(row, "id", "?"),
                )
                db.rollback()
    except Exception:
        summary["errors"] += 1
        logger.exception("semaforo: expired notice failed; live alerts are not affected")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _create_alerts(
    db: Session, row: CaseDeadline, today: date, existing: set, created: Dict[str, int]
) -> None:
    """Create (and commit) the alerts ``row`` is due on ``today``."""
    a = assess(row.deadline_type, row.triggered_at, today, _confirmed_term(row))
    if a is None or a.level is None:
        return
    entity_type = entity_type_for(a)
    alert_type = ALERT_TYPE_BY_LEVEL[a.level]

    case = db.get(Case, row.case_id)
    if case is None:
        return
    recipients = _recipients(db, case, a.level)
    if all((entity_type, row.id, alert_type, r.id) in existing for r in recipients):
        return
    if not _is_ours(db, case, row, today):
        return

    tribunal = case.court.name if case.court else "tribunal no especificado"
    title, message = build_alert_text(a, case.rol or "", tribunal)
    for recipient in recipients:
        key = (entity_type, row.id, alert_type, recipient.id)
        if key in existing:
            continue
        db.add(Alert(
            lawyer_id=recipient.id,
            case_id=case.id,
            type=alert_type,
            entity_type=entity_type,
            entity_id=row.id,
            title=title,
            message=message,
        ))
        existing.add(key)
        created[a.level.value] += 1
    db.commit()


def _confirmed_term(row: CaseDeadline) -> Optional[int]:
    return confirmed_excepciones_term(row) if row.deadline_type == EXCEPCIONES else None


def _dispatch_pending(db: Session, today: date, svc: NotificationService) -> int:
    """Send every plazo alert not yet emailed whose term is still running.

    This is both the first send and the retry: an alert that failed to leave
    stays ``email_sent=false`` and is picked up again here until its term is
    over. Urgent first. Verde is only queued (the caller closes ``svc``, which
    sends one grouped email per lawyer).
    """
    pending = (
        db.query(Alert)
        .join(CaseDeadline, CaseDeadline.id == Alert.entity_id)
        .filter(
            Alert.entity_type.like(f"{_ENTITY_PREFIX}%"),
            Alert.type.in_(SEMAFORO_ALERT_TYPES),
            Alert.email_sent.is_(False),
            CaseDeadline.status == "active",
            CaseDeadline.due_date >= today,
        )
        .all()
    )
    pending.sort(key=lambda al: -URGENCY.index(LEVEL_BY_ALERT_TYPE[al.type]))

    webhooks: Dict[int, List[Webhook]] = {}
    dispatched = 0
    for alert in pending:
        lawyer = db.get(Lawyer, alert.lawyer_id)
        if lawyer is None:
            continue
        if LEVEL_BY_ALERT_TYPE[alert.type] is Level.VERDE:
            svc.accumulate_plazo_verde(alert, lawyer)
        else:
            if lawyer.id not in webhooks:
                webhooks[lawyer.id] = (
                    db.query(Webhook)
                    .filter(Webhook.lawyer_id == lawyer.id, Webhook.is_active == True)  # noqa: E712
                    .all()
                )
            svc.notify_plazo_alert(alert, lawyer, webhooks[lawyer.id])
        dispatched += 1
    return dispatched


def _dispatch_expired(db: Session, today: date, svc: NotificationService) -> int:
    """Send every expired-and-unfiled alert not yet emailed.

    Its own rule, because by definition the term is over: ``_dispatch_pending``
    only sends while ``due_date >= today``. This one sends (and retries a failed
    send) while the plazo is still inside ``SEMAFORO_EXPIRED_WINDOW_DAYS``
    (<= 0 = no window), the case is still active and the verdict still says the
    firm did not act: a verdict that flipped to "cumplido" since the alert was
    raised cancels the pending email. Each alert is sent at most once
    (``email_sent``).
    """
    query = (
        db.query(Alert)
        .join(CaseDeadline, CaseDeadline.id == Alert.entity_id)
        .join(Case, Case.id == CaseDeadline.case_id)
        .filter(
            Alert.entity_type.like(f"{_ENTITY_PREFIX}%"),
            Alert.type == EXPIRED_ALERT_TYPE,
            Alert.email_sent.is_(False),
            CaseDeadline.verdict.in_(_NOT_ACTED_VERDICTS),
            CaseDeadline.due_date < today,
            Case.status == "active",
            ~medida_prejudicial_clause(),
        )
    )
    window = settings.SEMAFORO_EXPIRED_WINDOW_DAYS
    if window > 0:
        query = query.filter(CaseDeadline.due_date >= today - timedelta(days=window))

    webhooks: Dict[int, List[Webhook]] = {}
    dispatched = 0
    for alert in query.order_by(Alert.id).all():
        lawyer = db.get(Lawyer, alert.lawyer_id)
        if lawyer is None:
            continue
        if lawyer.id not in webhooks:
            webhooks[lawyer.id] = (
                db.query(Webhook)
                .filter(Webhook.lawyer_id == lawyer.id, Webhook.is_active == True)  # noqa: E712
                .all()
            )
        svc.notify_plazo_alert(alert, lawyer, webhooks[lawyer.id])
        dispatched += 1
    return dispatched


def run_semaforo_alerts(
    db: Session,
    today: Optional[date] = None,
    notification_svc: Optional[NotificationService] = None,
) -> Dict[str, Any]:
    """Raise and send every semaforo alert due ``today``. NEVER raises.

    Returns a summary: plazos ``evaluated``, alerts ``created`` per level,
    alerts ``dispatched`` (new + retried), ``errors`` and plazos
    ``suppressed_by_cutoff`` (see ``_within_cutoff``), ``expired_created`` and
    ``expired_dispatched`` (one-time "fatal plazo expired unfiled" alerts, a channel parallel to the
    semaforo; see ``expired_unfiled_deadlines``).
    """
    summary: Dict[str, Any] = {
        "evaluated": 0,
        "created": {"verde": 0, "amarillo": 0, "rojo": 0},
        "dispatched": 0,
        "errors": 0,
        "suppressed_by_cutoff": 0,
        "expired_created": 0,
        "expired_dispatched": 0,
    }
    owns_svc = notification_svc is None
    svc: Optional[NotificationService] = notification_svc
    try:
        today = today or _today_chile()
        rows = _candidates(db, today)
        existing = _existing_keys(db, [r.id for r in rows])
        for row in rows:
            if not _within_cutoff(row, today):
                summary["suppressed_by_cutoff"] += 1
                continue
            summary["evaluated"] += 1
            try:
                _create_alerts(db, row, today, existing, summary["created"])
            except Exception:
                summary["errors"] += 1
                logger.exception(
                    "semaforo: could not evaluate case_deadline %s; skipped",
                    getattr(row, "id", "?"),
                )
                db.rollback()

        if summary["suppressed_by_cutoff"]:
            logger.info(
                "semaforo: %d plazo(s) suppressed by the cutoff "
                "(SEMAFORO_MAX_TRIGGER_AGE_DAYS=%d; triggered earlier or unknown)",
                summary["suppressed_by_cutoff"],
                settings.SEMAFORO_MAX_TRIGGER_AGE_DAYS,
            )

        _run_expired_notice(db, today, summary)

        if svc is None:
            svc = NotificationService(db)
        summary["dispatched"] = _dispatch_pending(db, today, svc)
        try:
            summary["expired_dispatched"] = _dispatch_expired(db, today, svc)
        except Exception:
            summary["errors"] += 1
            logger.exception("semaforo: expired notice dispatch failed; skipped")
            db.rollback()
    except Exception:
        summary["errors"] += 1
        logger.exception("semaforo: run failed; the sync cycle is not affected")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if owns_svc and svc is not None:
            svc.close()  # sends the grouped verde emails; never raises
    return summary
