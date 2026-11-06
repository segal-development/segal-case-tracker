"""Recording helpers for the activity audit.

SECURITY (S2): ``insert_event`` only accepts the whitelisted columns of
``ActivityEvent``; nothing derived from bodies, query strings, headers (other
than a trimmed User-Agent) or tokens is ever passed in.
"""

import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_access_token
from app.models.activity_event import ActivityEvent
from app.models.lawyer import Lawyer

logger = logging.getLogger(__name__)

# Tests that do not exercise the audit turn this off (see tests/conftest.py).
ENABLED = True

_session_factory: Optional[Callable[[], Session]] = None

_FIELDS = {
    "lawyer_id", "actor_name", "actor_role", "kind", "method", "route", "path",
    "status_code", "case_id", "target", "ip", "user_agent", "identifier",
}


def set_session_factory(factory: Optional[Callable[[], Session]]) -> None:
    """Override the audit's own session factory (None restores SessionLocal)."""
    global _session_factory
    _session_factory = factory


def new_session() -> Session:
    return (_session_factory or SessionLocal)()


def _trim(value: Optional[str], n: int) -> Optional[str]:
    return value[:n] if value is not None else None


def insert_event(db: Session, **fields) -> ActivityEvent:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"unexpected activity fields: {sorted(unknown)}")
    for key, n in (("user_agent", 255), ("identifier", 120), ("route", 255),
                   ("path", 255), ("target", 255), ("ip", 64)):
        if key in fields:
            fields[key] = _trim(fields[key], n)
    ev = ActivityEvent(created_at=datetime.utcnow(), **fields)
    db.add(ev)
    db.commit()
    return ev


def lawyer_from_token(db: Session, authorization: Optional[str]) -> Optional[Lawyer]:
    """Resolve the Lawyer behind a Bearer token, or None if missing/invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    payload = decode_access_token(authorization[7:].strip())
    if not payload:
        return None
    sub = payload.get("sub") or payload.get("lawyer_id")
    if sub is None:
        return None
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        return db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    return db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()


def actor_fields(lawyer: Optional[Lawyer]) -> dict:
    if lawyer is None:
        return {"lawyer_id": None, "actor_name": None, "actor_role": None}
    return {"lawyer_id": lawyer.id, "actor_name": lawyer.name, "actor_role": lawyer.role}


def client_ip(headers: dict, fallback: str | None) -> str | None:
    """Browser IP for the audit. Requests reach the API through the Vercel proxy, so the ASGI
    client is Vercel; the browser is the first X-Forwarded-For hop (then X-Real-IP). Only valid
    IP literals are accepted. The header can be forged by a client that calls the API directly,
    so this is informative, not evidence."""
    import ipaddress

    candidates = []
    xff = headers.get("x-forwarded-for")
    if xff:
        candidates.append(xff.split(",")[0].strip())
    if headers.get("x-real-ip"):
        candidates.append(headers["x-real-ip"].strip())
    for value in candidates:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return fallback
