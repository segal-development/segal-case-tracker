"""Admin management of the external Redaccion API keys.

INTERNAL admin panel (protected by the app's admin JWT via ``require_admin``)
for the EXTERNAL ``RedaccionApiKey`` rows — so admins create/revoke the keys the
Redaccion (document-drafters) system uses in its ``X-API-Key`` header, instead of
running ``scripts/create_redaccion_key.py`` by hand.

Key generation mirrors that script exactly: ``secrets.token_urlsafe(32)`` for the
plaintext, ``hashlib.sha256(plain).hexdigest()`` persisted as ``key_hash``. The
plaintext is returned exactly ONCE on creation and never stored — it cannot be
recovered afterwards. ``key_hash`` is NEVER exposed by any endpoint.

These endpoints only manage the key rows; the external API auth itself lives in
``app.api.redaccion.deps.require_redaccion_key`` and is untouched here.
"""

import hashlib
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.redaccion_api_key import RedaccionApiKey

router = APIRouter()

_MAX_LABEL_LEN = 255


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class RedaccionKeyRow(BaseModel):
    """A Redaccion API key as shown in the admin panel. NEVER carries the hash
    or any plaintext."""

    id: int
    label: str
    is_active: bool
    revoked: bool
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class RedaccionKeyCreateIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=_MAX_LABEL_LEN)


class RedaccionKeyCreated(BaseModel):
    """Response for a freshly created key.

    ``key`` is the PLAINTEXT api key and is returned ONLY this one time, at
    creation — it is never stored (only its SHA-256 hash is) and cannot be
    recovered later. The admin UI must show it once and warn the user to save
    it now.
    """

    id: int
    label: str
    key: str
    created_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row(key: RedaccionApiKey) -> RedaccionKeyRow:
    return RedaccionKeyRow(
        id=key.id,
        label=key.label,
        is_active=bool(key.is_active),
        revoked=key.revoked_at is not None,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/redaccion-keys", response_model=List[RedaccionKeyRow])
async def list_redaccion_keys(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """List every Redaccion API key (newest first). Never exposes the hash."""
    keys = (
        db.query(RedaccionApiKey)
        .order_by(RedaccionApiKey.created_at.desc(), RedaccionApiKey.id.desc())
        .all()
    )
    return [_row(k) for k in keys]


@router.post("/redaccion-keys", response_model=RedaccionKeyCreated)
async def create_redaccion_key(
    body: RedaccionKeyCreateIn,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Create a new Redaccion API key.

    Generates ``secrets.token_urlsafe(32)`` and stores only its SHA-256 hash.
    Returns the plaintext ``key`` ONCE — it is never stored and cannot be
    recovered afterwards.
    """
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="El label no puede estar vacío")
    if len(label) > _MAX_LABEL_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"El label no puede superar {_MAX_LABEL_LEN} caracteres",
        )

    plaintext = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    key = RedaccionApiKey(
        label=label,
        key_hash=key_hash,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    return RedaccionKeyCreated(
        id=key.id, label=key.label, key=plaintext, created_at=key.created_at
    )


@router.post("/redaccion-keys/{key_id}/revoke", response_model=RedaccionKeyRow)
async def revoke_redaccion_key(
    key_id: int,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Revoke a key: ``is_active=False`` + ``revoked_at=utcnow()``.

    404 if the id doesn't exist. Idempotent: revoking an already-revoked key
    just returns its current (revoked) state.
    """
    key = db.query(RedaccionApiKey).filter(RedaccionApiKey.id == key_id).first()
    if key is None:
        raise HTTPException(status_code=404, detail="Key no encontrada")

    if key.is_active or key.revoked_at is None:
        key.is_active = False
        if key.revoked_at is None:
            key.revoked_at = datetime.utcnow()
        db.commit()
        db.refresh(key)

    return _row(key)
