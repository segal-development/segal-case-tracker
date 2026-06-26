"""DB-backed helpers for the PJUD one-click connection queue.

Replaces the previous Redis-backed implementation.  All functions are now
synchronous and accept a SQLAlchemy Session instead of a Redis client.

Security contract: no password or credential field is ever stored.  Only
{connection_id, lawyer_id, rut, auth_method, captcha_token|null} is
persisted — captcha_token is an ephemeral reCAPTCHA v3 value (~120 s),
not a replayable credential (INV-1 / INV-4).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.pending_connection import PendingConnection

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (stored without tz in DB)."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def enqueue_connection(
    db: Session,
    *,
    lawyer_id: int,
    rut: str,
    auth_method: str,
    captcha_token: Optional[str] = None,
) -> str:
    """Insert a credential-free connection request row with status='pending'.

    Args:
        db: SQLAlchemy session (caller owns commit behaviour; this fn commits).
        lawyer_id: DB primary key of the requesting lawyer.
        rut: Lawyer RUT in normalized form (e.g. "12345678-9").
        auth_method: ``"segunda_clave"`` or ``"clave_unica"``.
        captcha_token: reCAPTCHA v3 token; required for segunda_clave, None for CU.

    Returns:
        The generated ``connection_id`` (UUID4, 36 chars).
    """
    connection_id = str(uuid4())
    now = _utcnow()
    row = PendingConnection(
        connection_id=connection_id,
        lawyer_id=lawyer_id,
        rut=rut,
        auth_method=auth_method,
        captcha_token=captcha_token,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "Enqueued connection job %s for lawyer %d (method=%s)",
        connection_id,
        lawyer_id,
        auth_method,
    )
    return connection_id


def dequeue_connection(db: Session) -> Optional[Dict[str, Any]]:
    """Claim the oldest pending row atomically.

    Uses SELECT … FOR UPDATE SKIP LOCKED on Postgres so multiple workers
    cannot double-pick the same row.  Falls back to a plain SELECT on
    SQLite (used in tests), where row-level locking is not supported.

    Returns:
        Job dict {connection_id, lawyer_id, rut, auth_method, captcha_token}
        or ``None`` when no pending rows exist.
    """
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        dialect = "unknown"

    query = (
        db.query(PendingConnection)
        .filter(PendingConnection.status == "pending")
        .order_by(PendingConnection.created_at.asc())
        .limit(1)
    )
    if dialect == "postgresql":
        query = query.with_for_update(skip_locked=True)

    row = query.first()
    if row is None:
        return None

    now = _utcnow()
    row.status = "connecting"
    row.picked_at = now
    row.updated_at = now
    db.commit()

    return {
        "connection_id": row.connection_id,
        "lawyer_id": row.lawyer_id,
        "rut": row.rut,
        "auth_method": row.auth_method,
        "captcha_token": row.captcha_token,
    }


def set_status(db: Session, connection_id: str, **fields: Any) -> None:
    """Update the pending_connections row for ``connection_id``.

    Accepted keyword arguments: status, session_id, cases_synced, error.
    ``updated_at`` is always refreshed.

    Args:
        db: SQLAlchemy session.
        connection_id: UUID4 string returned by ``enqueue_connection``.
        **fields: Column values to set on the row.
    """
    row = (
        db.query(PendingConnection)
        .filter(PendingConnection.connection_id == connection_id)
        .first()
    )
    if row is None:
        logger.warning("set_status: no row found for connection_id=%s", connection_id)
        return

    allowed = {"status", "session_id", "cases_synced", "error"}
    for key, value in fields.items():
        if key in allowed:
            setattr(row, key, value)
    row.updated_at = _utcnow()
    db.commit()


def get_status(db: Session, connection_id: str) -> Optional[Dict[str, Any]]:
    """Return the current status dict for ``connection_id``, or None if not found.

    Args:
        db: SQLAlchemy session.
        connection_id: UUID4 string to look up.

    Returns:
        Dict with keys {status, session_id, cases_synced, error, updated_at}
        or ``None``.
    """
    row = (
        db.query(PendingConnection)
        .filter(PendingConnection.connection_id == connection_id)
        .first()
    )
    if row is None:
        return None
    return {
        "status": row.status,
        "session_id": row.session_id,
        "cases_synced": row.cases_synced,
        "error": row.error,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
