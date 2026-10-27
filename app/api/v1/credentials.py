"""Credential-monitoring endpoints ("bóveda de credenciales") — read-only, auditor-only.

SECURITY: responses expose ONLY safe metadata (booleans, timestamps, enum/status
strings, counts). No credential value, plaintext, ciphertext, or fingerprint is
ever returned. The response models below pin the exact safe shape so no extra
field can leak through.
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import (
    _resolve_lawyer_id,
    get_current_lawyer,
    get_db,
    require_admin,
    require_auditor,
)
from app.models.lawyer import Lawyer
from app.core.security import encrypt_pjud_password
from app.models.credential_audit_event import CredentialAuditEvent
from app.services.credential_audit import (
    _fingerprint,
    credential_status,
    scan_credential_changes,
)

router = APIRouter()


class CredentialSubStatus(BaseModel):
    """Safe per-credential-type status — no value/ciphertext/fingerprint."""

    present: bool
    health: str  # "valid" | "failing" | "pending_validation" | "never_validated"
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


class SetLawyerCredentialRequest(BaseModel):
    """A key an administrator loads for ANOTHER lawyer."""

    password: str = Field(min_length=4, max_length=200)


#: URL system -> (encrypted column, credential_audit type, preferred_auth_method it implies).
_ADMIN_SLOTS = {
    "pjud": ("encrypted_pjud_password", "pjud", "captcha"),
    "clave_unica": ("encrypted_clave_unica_password", "clave_unica", "clave_unica"),
}


@router.put("/lawyers/{lawyer_id}/{system}", status_code=200)
def set_lawyer_credential(
    lawyer_id: int,
    system: Literal["pjud", "clave_unica"],
    body: SetLawyerCredentialRequest,
    admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """An ADMIN loads or changes the key of another lawyer (admin only, not auditors).

    Same storage as the self-service endpoint (Fernet-encrypted, never echoed, logged
    or returned) with two differences that matter when it is somebody else's key:

    * ``preferred_auth_method`` is changed ONLY when the slot it points to has no key
      yet (the column defaults to captcha, so it is never empty). The worker
      re-authenticates through it: loading the OTHER slot of a lawyer who already has
      a working key must not silently change how that lawyer logs in, but a lawyer with
      nothing stored needs the method of the key just loaded or the worker would try an
      empty slot and skip them.
    * an append-only ``credential_audit_events`` row records WHO did it (the admin's
      lawyer id, never a credential) next to the ciphertext fingerprint.

    Like the self-service path it does not validate live (a PJUD login from the server
    is blocked): the next scrape cycle tests it. Resetting ``credential_alert_sent_at``
    lets a future failure notify again.
    """
    lawyer = db.get(Lawyer, lawyer_id)
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()

    column, credential_type, implied_method = _ADMIN_SLOTS[system]
    encrypted = encrypt_pjud_password(body.password)
    setattr(lawyer, column, encrypted)
    if system == "clave_unica":
        lawyer.clave_unica_rut = str(lawyer.rut)  # type: ignore[assignment]
    preferred = _ADMIN_SLOTS.get(
        "clave_unica" if lawyer.preferred_auth_method == "clave_unica" else "pjud"
    )
    if not getattr(lawyer, preferred[0]):  # type: ignore[index]
        lawyer.preferred_auth_method = implied_method  # type: ignore[assignment]
    lawyer.credential_alert_sent_at = None  # a future failure alerts again

    db.add(
        CredentialAuditEvent(
            lawyer_id=int(lawyer.id),
            credential_type=credential_type,
            event_type="value_changed",
            fingerprint=_fingerprint(encrypted),
            detail=f"cargada por el administrador (id {admin.id if admin else '?'})",
            occurred_at=datetime.utcnow(),
        )
    )
    db.commit()
    return {"ok": True, "system": system}
