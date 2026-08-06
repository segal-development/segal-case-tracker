"""SysgalApiKey model - per-consumer API keys for the external Sysgal CRM API.

Mirrors ``app.models.ingest_key.IngestKey``: only the SHA-256 hash of the key
is stored, never the plaintext. Distinct table and dependency so the external
read-only Sysgal API is fully isolated from the internal app's ingest auth.

See ``app.api.sysgal.deps.require_sysgal_key`` for the lookup/validation logic
and ``scripts/create_sysgal_key.py`` for generation.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class SysgalApiKey(Base):
    """Hashed API key authorizing the Sysgal CRM to call the external read-only API."""

    __tablename__ = "sysgal_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
