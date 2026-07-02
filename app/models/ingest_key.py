"""IngestKey model - per-operator API keys for the PJUD extension ingest endpoints.

Mirrors the ``lawyers.password_hash`` pattern: only the SHA-256 hash of the
key is stored, never the plaintext. See ``app.api.deps.require_ingest_key``
for the lookup/validation logic and ``scripts/manage_ingest_key.py`` for
generation/rotation/revocation.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class IngestKey(Base):
    """Hashed API key authorizing the browser extension to call ingest endpoints."""

    __tablename__ = "ingest_keys"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
