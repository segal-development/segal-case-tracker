"""API Dependencies - Authentication, DB Session, etc."""

from typing import Generator, Optional
from fastapi import Depends, HTTPException, status
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
    return int(lawyer.id)
