"""Auth dependency for the external Sysgal CRM API.

Mirrors ``app.api.deps.require_ingest_key`` but for the isolated Sysgal API:
reads the ``X-API-Key`` header, hashes it (SHA-256, same scheme as
``IngestKey``), and validates it against an active, non-revoked
``SysgalApiKey`` row. Machine-to-machine credential, entirely separate from
lawyer JWT auth and from the internal ingest key.
"""

import hashlib
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db


def require_sysgal_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """Validate the ``X-API-Key`` header used by the Sysgal CRM.

    The key is stored hashed (SHA-256) — never in plaintext — so lookup is a
    direct equality match. Raises 401 when the header is missing/empty or when
    it doesn't match any active, non-revoked key. On success, stamps
    ``last_used_at`` and returns the matched ``SysgalApiKey`` row.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    from app.models.sysgal_api_key import SysgalApiKey

    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    key = db.query(SysgalApiKey).filter(SysgalApiKey.key_hash == key_hash).first()

    if key is None or not key.is_active or key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    key.last_used_at = datetime.utcnow()  # type: ignore[assignment]
    db.commit()
    return key
