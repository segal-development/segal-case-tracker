"""Canonical PJUD session value object.

Single source of truth for PJUDSession — no Redis or Playwright imports,
which keeps this module importable from any layer without creating cycles.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


logger = logging.getLogger(__name__)

SESSION_EXPIRY_MINUTES = 25


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=timezone.utc)


@dataclass
class PJUDSession:
    """
    Canonical PJUD session.  Used by both the scraper (login) and the
    async session store (worker).

    Timestamps are always UTC-aware (``tzinfo=timezone.utc``).
    """

    session_id: str
    lawyer_id: int
    rut: str
    cookies: List[Dict[str, Any]]
    created_at: datetime
    expires_at: datetime
    local_storage: str = "{}"
    last_used_at: Optional[datetime] = None
    auth_method: str = "captcha"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        rut: str,
        cookies: List[Dict[str, Any]],
        *,
        lawyer_id: int = 0,
        local_storage: str = "{}",
        auth_method: str = "captcha",
    ) -> "PJUDSession":
        """Create a new session with UTC-aware timestamps and a uuid4 ID.

        Args:
            rut: Lawyer RUT (normalized form, e.g. "12345678-9").
            cookies: Playwright cookies list.
            lawyer_id: Resolved DB id; defaults to 0 until Slice 2 wires
                       ``_get_or_create_lawyer``.
            local_storage: Serialised localStorage JSON string.
            auth_method: "captcha" or "clave_unica".
        """
        now = _utcnow()
        return cls(
            session_id=str(uuid4()),
            lawyer_id=lawyer_id,
            rut=rut,
            cookies=cookies,
            created_at=now,
            expires_at=now + timedelta(minutes=SESSION_EXPIRY_MINUTES),
            local_storage=local_storage,
            auth_method=auth_method,
        )

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def is_expired(self) -> bool:
        """Return True if the session has passed its expiry time (UTC)."""
        return _utcnow() > self.expires_at

    def time_until_expiry(self) -> timedelta:
        """Time remaining before the session expires (may be negative)."""
        return self.expires_at - _utcnow()

    # ------------------------------------------------------------------
    # Redis serialisation
    # ------------------------------------------------------------------

    def to_redis(self) -> str:
        """Serialise to a JSON string suitable for Redis storage."""
        return json.dumps({
            "session_id": self.session_id,
            "lawyer_id": self.lawyer_id,
            "rut": self.rut,
            "cookies": self.cookies,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "local_storage": self.local_storage,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "auth_method": self.auth_method,
        })

    @classmethod
    def from_redis(cls, raw: str) -> "PJUDSession":
        """Deserialise from a JSON string retrieved from Redis.

        Naive ISO strings (no tzinfo) are treated as UTC for backward
        compatibility with data that was written before the canonical model.
        """
        data = json.loads(raw)

        def _parse_dt(value: str) -> datetime:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        last_used_at: Optional[datetime] = None
        if data.get("last_used_at"):
            last_used_at = _parse_dt(data["last_used_at"])

        return cls(
            session_id=data["session_id"],
            lawyer_id=data["lawyer_id"],
            rut=data["rut"],
            cookies=data["cookies"],
            created_at=_parse_dt(data["created_at"]),
            expires_at=_parse_dt(data["expires_at"]),
            local_storage=data.get("local_storage", "{}"),
            last_used_at=last_used_at,
            auth_method=data.get("auth_method", "captcha"),
        )
