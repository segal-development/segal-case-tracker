"""Auth dependency for the external Presentación (GEDOC escrito-upload) API.

Mirrors ``app.api.redaccion.deps.require_redaccion_key`` but for the isolated
Presentación API: reads the ``X-API-Key`` header, hashes it (SHA-256, same
scheme as ``RedaccionApiKey`` / ``SysgalApiKey`` / ``IngestKey``), and validates
it against an active, non-revoked ``PresentacionApiKey`` row. Machine-to-machine
credential, entirely separate from lawyer JWT auth, from the internal ingest
key, from the Sysgal API key, AND from the Redaccion API key.
"""

import hashlib
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db


def require_presentacion_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """Validate the ``X-API-Key`` header used by the GEDOC system.

    The key is stored hashed (SHA-256) — never in plaintext — so lookup is a
    direct equality match. Raises 401 when the header is missing/empty or when
    it doesn't match any active, non-revoked key. On success, stamps
    ``last_used_at`` and returns the matched ``PresentacionApiKey`` row.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    from app.models.presentacion_api_key import PresentacionApiKey

    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    key = (
        db.query(PresentacionApiKey)
        .filter(PresentacionApiKey.key_hash == key_hash)
        .first()
    )

    if key is None or not key.is_active or key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    key.last_used_at = datetime.utcnow()  # type: ignore[assignment]
    db.commit()
    return key
