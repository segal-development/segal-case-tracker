"""In-memory PJUD request/rejection counters, flushed to ``pjud_request_counts``.

Measurement only: nothing here may change pacing, retries, budgets, the egress
guard or any scraping behaviour. Every public function swallows its own errors
(logged once) so a counting failure never reaches the scraper.

* ``record_request`` / ``record_rejection`` are synchronous, do no I/O and hold a
  plain lock only around a dict update, so they are safe from any task or thread.
* The account comes from a ``ContextVar`` set by the scheduler per lawyer unit
  (``set_lawyer``); the egress IP is read from the egress guard's existing cache
  (never a new lookup).
* ``flush(db)`` upserts the counters into hourly UTC buckets and subtracts only
  what it flushed, so a crash loses at most the unflushed counts and a retry after
  a failed flush never double counts.
* ``flush`` sends at most one Telegram per (IP, rejection kind) per hour.
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

KIND_REQUEST = "request"
KIND_SHAPE = "shape_challenge"
KIND_HTTP_403 = "http_403"
KIND_HTTP_429 = "http_429"
KIND_SESSION_BLOCKED = "session_blocked"
KIND_CONNECTION_RESET = "connection_reset"

UNKNOWN_IP = "unknown"
ALERT_REPEAT_SECONDS = 60 * 60

#: Wall clock (UTC); monkeypatch in tests.
NOW: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731

_current_lawyer: ContextVar[Optional[int]] = ContextVar("pjud_metrics_lawyer", default=None)

Key = tuple  # (hour_utc, lawyer_id, egress_ip, action, kind)
_counts: dict[Key, int] = {}
_lock = threading.Lock()
_last_alert: dict[tuple[str, str], datetime] = {}
_failure_logged = False


def reset_state() -> None:
    """Forget counters and alert state (tests)."""
    global _failure_logged
    with _lock:
        _counts.clear()
        _last_alert.clear()
    _failure_logged = False


def set_lawyer(lawyer_id: Optional[int]) -> Token:
    return _current_lawyer.set(lawyer_id)


def reset_lawyer(token: Token) -> None:
    try:
        _current_lawyer.reset(token)
    except ValueError:  # token from another context; never break the caller
        _current_lawyer.set(None)


def _log_failure_once(what: str) -> None:
    global _failure_logged
    if not _failure_logged:
        _failure_logged = True
        logger.exception("PJUD metrics: %s failed (non-fatal, logged once)", what)


def _cached_egress_ip() -> str:
    """Last IP the egress guard cached, whatever its age. Never looks one up."""
    try:
        from app.services import egress_guard

        cache = egress_guard._cache
        ip = cache[1][1] if cache else None
        return ip or UNKNOWN_IP
    except Exception:  # noqa: BLE001
        return UNKNOWN_IP


def _hour(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)


def _bump(action: str, kind: str) -> None:
    try:
        key = (_hour(NOW()), _current_lawyer.get(), _cached_egress_ip(), str(action), kind)
        with _lock:
            _counts[key] = _counts.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        _log_failure_once("record")


def record_request(action: str) -> None:
    _bump(action, KIND_REQUEST)


def record_rejection(kind: str, action: str) -> None:
    _bump(action, kind)


def record_http_status(status: object, action: str) -> None:
    """Count only 403/429 answers; any other status is not a throttling signal."""
    try:
        code = int(status)  # type: ignore[call-overload]
    except Exception:  # noqa: BLE001 — TypeError/ValueError/OverflowError...
        return
    if code == 403:
        record_rejection(KIND_HTTP_403, action)
    elif code == 429:
        record_rejection(KIND_HTTP_429, action)


def _upsert(db: Session, snapshot: dict[Key, int]) -> None:
    from app.models.pjud_request_count import PjudRequestCount as T

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    for (hour, lawyer_id, ip, action, kind), n in snapshot.items():
        if lawyer_id is None:
            # NULLs never conflict in a unique constraint: update-then-insert.
            res = db.execute(
                update(T)
                .where(T.hour_utc == hour, T.lawyer_id.is_(None), T.egress_ip == ip,
                       T.action == action, T.kind == kind)
                .values(count=T.count + n)
            )
            if res.rowcount:
                continue
        stmt = insert(T).values(
            hour_utc=hour, lawyer_id=lawyer_id, egress_ip=ip, action=action, kind=kind, count=n
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["hour_utc", "lawyer_id", "egress_ip", "action", "kind"],
            set_={"count": T.count + stmt.excluded.count},
        )
        db.execute(stmt)


def _alert(snapshot: dict[Key, int]) -> None:
    from app.services.telegram_notifier import send_telegram

    now = NOW()
    groups: dict[tuple[str, str], dict[Optional[int], int]] = {}
    for (_h, lawyer_id, ip, _a, kind), n in snapshot.items():
        if kind == KIND_REQUEST:
            continue
        by_acc = groups.setdefault((ip, kind), {})
        by_acc[lawyer_id] = by_acc.get(lawyer_id, 0) + n
    for (ip, kind), by_acc in groups.items():
        last = _last_alert.get((ip, kind))
        if last is not None and (now - last) < timedelta(seconds=ALERT_REPEAT_SECONDS):
            continue
        _last_alert[(ip, kind)] = now
        accounts = ", ".join(f"{a if a is not None else 'n/a'}: {c}" for a, c in sorted(
            by_acc.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))
        try:
            send_telegram(
                f"PJUD rechazo: {kind} x{sum(by_acc.values())} desde IP {ip} "
                f"(cuenta: {accounts})."
            )
        except Exception:  # noqa: BLE001 — an alert never breaks the worker
            logger.warning("PJUD metrics alert could not be sent")


def flush(db: Session) -> None:
    """Upsert the counters into the DB; never raises, never double counts."""
    try:
        with _lock:
            snapshot = dict(_counts)
        if not snapshot:
            return
        try:
            _upsert(db, snapshot)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        with _lock:
            for key, n in snapshot.items():
                left = _counts.get(key, 0) - n
                if left > 0:
                    _counts[key] = left
                else:
                    _counts.pop(key, None)
        _alert(snapshot)
    except Exception:  # noqa: BLE001
        _log_failure_once("flush")
