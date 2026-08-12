"""RedaccionApiKey model - per-consumer API keys for the external Redaccion API.

Mirrors ``app.models.sysgal_api_key.SysgalApiKey`` (which itself mirrors
``app.models.ingest_key.IngestKey``): only the SHA-256 hash of the key is
stored, never the plaintext. Distinct table and dependency so the external
read-only Redaccion (document-drafters) API is fully isolated from both the
internal app's ingest auth and the Sysgal CRM API. Independently revocable.

See ``app.api.redaccion.deps.require_redaccion_key`` for the lookup/validation
logic and ``scripts/create_redaccion_key.py`` for generation.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class RedaccionApiKey(Base):
    """Hashed API key authorizing the Redaccion system to call the external read-only API."""

    __tablename__ = "redaccion_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
