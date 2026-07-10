"""CredentialAuditEvent model — append-only audit trail for credential monitoring.

SECURITY:
This table NEVER stores a credential value. The only credential-derived column
is ``fingerprint`` = ``sha256(ciphertext)`` — a hash of the ALREADY-ENCRYPTED
blob (``lawyers.encrypted_*_password``). A hash of ciphertext reveals nothing
about the plaintext and is safe to store/compare. No plaintext is ever decrypted
to populate this table.
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey

from app.core.database import Base


class CredentialAuditEvent(Base):
    """One append-only row per observed credential event (validation / change)."""

    __tablename__ = "credential_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False, index=True)

    # "pjud" | "clave_unica"
    credential_type = Column(String(20), nullable=False)

    # "value_changed" | "validation_failed" | "validation_ok"
    event_type = Column(String(30), nullable=False)

    # sha256(ciphertext) hex — only set for "value_changed". NEVER a credential.
    fingerprint = Column(String(64), nullable=True)

    # Opaque detail (e.g. the _reauth reason string). NEVER a credential value.
    detail = Column(String(255), nullable=True)

    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
