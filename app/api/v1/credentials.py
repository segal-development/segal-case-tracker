"""Credential-monitoring endpoints ("bóveda de credenciales") — read-only, auditor-only.

SECURITY: responses expose ONLY safe metadata (booleans, timestamps, enum/status
strings, counts). No credential value, plaintext, ciphertext, or fingerprint is
ever returned. The response models below pin the exact safe shape so no extra
field can leak through.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, require_auditor, _resolve_lawyer_id
from app.models.lawyer import Lawyer
from app.services.credential_audit import credential_status, scan_credential_changes

router = APIRouter()


class CredentialSubStatus(BaseModel):
    """Safe per-credential-type status — no value/ciphertext/fingerprint."""

    present: bool
    health: str  # "valid" | "failing" | "never_validated"
    last_validation_ok_at: Optional[datetime]
    last_failed_at: Optional[datetime]
    last_changed_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class LawyerCredentialStatus(BaseModel):
    """Safe per-lawyer credential status entry."""

    lawyer_id: int
    lawyer_name: str
    lawyer_rut: str
    preferred_auth_method: Optional[str]
    pjud: CredentialSubStatus
    clave_unica: CredentialSubStatus

    model_config = ConfigDict(from_attributes=True)


class ScanResult(BaseModel):
    recorded: int


@router.get("/status", response_model=list[LawyerCredentialStatus])
def get_credential_status(
    _auditor: str = Depends(require_auditor),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return the read-only credential health of every lawyer (auditor-only)."""
    return credential_status(db)


@router.post("/scan", response_model=ScanResult)
def scan_credentials(
    _auditor: str = Depends(require_auditor),
    db: Session = Depends(get_db),
) -> dict:
    """Manually trigger ciphertext-fingerprint change detection (auditor-only)."""
    return {"recorded": scan_credential_changes(db)}


class UpdateMyCredentialRequest(BaseModel):
    """New PJUD credential submitted by the lawyer for themselves."""

    password: str = Field(min_length=4, max_length=200)
    # Which PJUD auth slot to update; defaults to the lawyer's current method.
    auth_method: Optional[str] = None

    @field_validator("auth_method")
    @classmethod
    def _valid_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("clave_unica", "captcha"):
            raise ValueError("auth_method debe ser 'clave_unica' o 'captcha'")
        return v


@router.put("/me", status_code=200)
def update_my_credential(
    body: UpdateMyCredentialRequest,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
) -> dict:
    """Self-service: the authenticated lawyer updates THEIR OWN PJUD credential.

    The clave is stored Fernet-encrypted (reusing the auth flow's storage) and
    NOT validated live here — validating means a real PJUD login, which from the
    server's datacenter IP is Shape-blocked. Instead the next scrape cycle (run
    from the residential IP) tests it, and the supervisor credential alert
    re-fires if it is still wrong. Resetting ``credential_alert_sent_at`` lets a
    future failure notify again. The plaintext is never persisted, logged, or
    returned.
    """
    # Imported here to avoid an import cycle at module load (auth.py is heavy).
    from app.api.v1.auth import _store_encrypted_credentials

    lawyer = db.get(Lawyer, _resolve_lawyer_id(db, current_lawyer))
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")

    method = body.auth_method or lawyer.preferred_auth_method or "clave_unica"
    _store_encrypted_credentials(db, lawyer, body.password, method)
    lawyer.credential_alert_sent_at = None  # a future failure alerts again
    db.commit()
    return {"ok": True, "auth_method": method}
