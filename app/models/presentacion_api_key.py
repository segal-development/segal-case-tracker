"""PresentacionApiKey model - per-consumer API keys for the external Presentación (GEDOC escrito-upload) API.

Mirrors ``app.models.redaccion_api_key.RedaccionApiKey`` (which itself mirrors
``app.models.sysgal_api_key.SysgalApiKey`` / ``app.models.ingest_key.IngestKey``):
only the SHA-256 hash of the key is stored, never the plaintext. Distinct table
and dependency so the external Presentación (GEDOC escrito-upload) API is fully
isolated from the internal app's ingest auth, the Sysgal CRM API, AND the
Redaccion API. Independently revocable.

See ``app.api.presentacion.deps.require_presentacion_key`` for the
lookup/validation logic and ``scripts/create_presentacion_key.py`` for generation.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class PresentacionApiKey(Base):
    """Hashed API key authorizing the GEDOC system to call the external Presentación API."""

    __tablename__ = "presentacion_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
