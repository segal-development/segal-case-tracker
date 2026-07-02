"""API Dependencies - Authentication, DB Session, etc."""

import hashlib
from datetime import datetime
from typing import Generator, Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_access_token

security = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    """Database session dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_current_lawyer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
):
    """Get current authenticated lawyer from JWT token."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authentication provided",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = credentials.credentials
    payload = decode_access_token(token)
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # TODO: Fetch lawyer from database using payload["sub"]
    # For now, return the payload
    return payload


async def require_admin(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
) -> str:
    """Allow access only to lawyers with role='admin'.

    Works with real JWT payloads (sub = RUT string) and test mocks
    (sub = numeric lawyer id string).  Returns the lawyer's RUT string.
    Raises 403 if the resolved lawyer is missing or not an admin.
    """
    from app.models.lawyer import Lawyer

    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Handle numeric id (used by test mocks) or RUT string (real JWT)
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        lawyer = db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    else:
        lawyer = db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()

    if lawyer is None or lawyer.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requiere rol admin")

    return str(lawyer.rut)


async def require_auditor(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
) -> str:
    """Allow access only to lawyers with role in {'auditor', 'admin'}.

    Admins can do everything auditors can.  Returns the lawyer's RUT string.
    Raises 403 if the resolved lawyer is missing or does not have an allowed role.
    """
    from app.models.lawyer import Lawyer

    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        lawyer = db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    else:
        lawyer = db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()

    if lawyer is None or lawyer.role not in {"auditor", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requiere rol auditor o admin",
        )

    return str(lawyer.rut)


def require_ingest_key(
    x_ingest_key: Optional[str] = Header(default=None, alias="X-Ingest-Key"),
    db: Session = Depends(get_db),
):
    """Validate the ``X-Ingest-Key`` header used by the PJUD browser extension.

    Machine-to-machine credential, distinct from lawyer JWT auth: one
    operator key authorizes ingest calls for all lawyers it manages. The
    key is stored hashed (SHA-256, mirrors ``lawyers.password_hash``) —
    never in plaintext — so lookup is a direct equality match, not a
    per-row bcrypt verify.

    Raises 401 when the header is missing/empty, 403 when it doesn't match
    any active key (unknown or revoked). On success, stamps
    ``last_used_at`` and returns the matched ``IngestKey`` row.
    """
    if not x_ingest_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Ingest-Key header",
        )

    from app.models.ingest_key import IngestKey

    key_hash = hashlib.sha256(x_ingest_key.encode()).hexdigest()
    key = db.query(IngestKey).filter(IngestKey.key_hash == key_hash).first()

    if key is None or not key.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or revoked ingest key",
        )

    key.last_used_at = datetime.utcnow()  # type: ignore[assignment]
    db.commit()
    return key


def _resolve_lawyer_id(db: Session, current_lawyer: dict) -> int:
    """Resolve the numeric lawyer id from a JWT payload.

    The JWT ``sub`` is the lawyer RUT (e.g. "16021492-9"); legacy/test tokens
    may carry the numeric id directly. Map the RUT to ``lawyers.id`` via the DB.
    Raises 401 when no subject is present and 404 when the lawyer is unknown.
    """
    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        return int(sub)
    from app.models.lawyer import Lawyer

    lawyer = db.query(Lawyer).filter(Lawyer.rut == sub).first()
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    # The auditor is a transversal role: it operates over the firm's full
    # caseload, not its own (it has no cases of its own). Resolve to the firm
    # account so the auditor can see/manage every study case and its deadlines.
    if getattr(lawyer, "role", None) == "auditor":
        import os

        firm_rut = os.environ.get("FIRM_LAWYER_RUT", "16021492-9")
        firm = db.query(Lawyer).filter(Lawyer.rut == firm_rut).first()
        if firm is not None:
            return int(firm.id)
    return int(lawyer.id)
