"""Redis helpers for the PJUD one-click connection queue.

Key schema (from design ADR):
  pjud:connect:queue                — list; LPUSH on enqueue, BLPOP on consume
  pjud:connect:status:{connection_id} — string; JSON status blob, TTL 300s

Security contract: the queue payload NEVER contains any credential or
password field.  Only {connection_id, lawyer_id, rut, auth_method,
captcha_token|null, enqueued_at} traverses the wire (INV-1 / INV-4).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Queue key (Redis list)
_QUEUE_KEY = "pjud:connect:queue"

# Status key prefix; full key is f"{_STATUS_PREFIX}{connection_id}"
_STATUS_PREFIX = "pjud:connect:status:"

# Status key TTL matches the JWT/captcha validity window (300 s)
_STATUS_TTL = 300


def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


async def enqueue_connection(
    redis: Any,
    *,
    lawyer_id: int,
    rut: str,
    auth_method: str,
    captcha_token: Optional[str] = None,
) -> str:
    """Push a credential-free connection job onto the Redis queue.

    Generates a UUID4 connection_id, builds the payload (no password
    fields), and LPUSH-es it onto ``pjud:connect:queue``.

    Args:
        redis: async Redis client (``redis.asyncio.Redis`` or compatible).
        lawyer_id: DB primary key of the requesting lawyer.
        rut: Lawyer's RUT in normalized form (e.g. "12345678-9").
        auth_method: ``"segunda_clave"`` or ``"clave_unica"``.
        captcha_token: reCAPTCHA v3 token; required for segunda_clave, None for CU.

    Returns:
        The generated ``connection_id`` (UUID4 string).
    """
    connection_id = str(uuid4())
    payload: Dict[str, Any] = {
        "connection_id": connection_id,
        "lawyer_id": lawyer_id,
        "rut": rut,
        "auth_method": auth_method,
        "captcha_token": captcha_token,
        "enqueued_at": _utcnow_iso(),
    }
    await redis.lpush(_QUEUE_KEY, json.dumps(payload))
    logger.info(
        "Enqueued connection job %s for lawyer %d (method=%s)",
        connection_id,
        lawyer_id,
        auth_method,
    )
    return connection_id


async def blpop_connection(
    redis: Any,
    timeout: int = 5,
) -> Optional[Dict[str, Any]]:
    """Block-pop one job from the connection queue.

    Args:
        redis: async Redis client.
        timeout: seconds to block; 0 = block indefinitely.

    Returns:
        Parsed job dict or ``None`` on timeout.
    """
    result = await redis.blpop(_QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    _key, raw = result
    return json.loads(raw)


async def set_status(
    redis: Any,
    connection_id: str,
    payload: Dict[str, Any],
) -> None:
    """Write (or overwrite) the status blob for ``connection_id``.

    Always sets a TTL of exactly 300 seconds.

    Args:
        redis: async Redis client.
        connection_id: UUID4 string returned by ``enqueue_connection``.
        payload: dict with at minimum ``{"status": ..., "updated_at": ...}``.
    """
    key = f"{_STATUS_PREFIX}{connection_id}"
    await redis.setex(key, _STATUS_TTL, json.dumps(payload))


async def get_status(
    redis: Any,
    connection_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the current status dict for ``connection_id``, or None if gone/expired.

    Args:
        redis: async Redis client.
        connection_id: UUID4 string to look up.

    Returns:
        Parsed status dict or ``None``.
    """
    key = f"{_STATUS_PREFIX}{connection_id}"
    raw = await redis.get(key)
    if raw is None:
        return None
    return json.loads(raw)
