"""Credential-monitoring endpoints ("bóveda de credenciales") — read-only, auditor-only.

SECURITY: responses expose ONLY safe metadata (booleans, timestamps, enum/status
strings, counts). No credential value, plaintext, ciphertext, or fingerprint is
ever returned. The response models below pin the exact safe shape so no extra
field can leak through.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_auditor
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
