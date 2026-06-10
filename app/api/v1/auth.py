"""
Authentication endpoints.

Two login methods supported (ADR-4):
1. Captcha-based:   RUT + Password + reCAPTCHA token → login_with_token
2. Clave Única:     RUT + Password via Chilean digital identity

Session refresh is intentionally removed (AUTH-03 / ADR-4).
Use the original login endpoint to re-authenticate.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_current_lawyer
from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError
from app.scrapper.pjud.browser import BrowserFactory
from app.scrapper.pjud.clave_unica import (
    ClaveUnicaAuth,
    ClaveUnicaCredentials,
    ClaveUnicaAuthError,
)
from app.services.session_store import get_session_store
from app.models.lawyer import Lawyer
from app.api.deps import get_db
from app.utils.rut import normalize_rut


logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# SCHEMAS
# ============================================================================

class LoginRequest(BaseModel):
    """Captcha login request."""
    rut: str = Field(..., description="RUT con dígito verificador (ej: 16021492-9)")
    password: str = Field(..., description="Clave Poder Judicial")
    captcha_token: str = Field(..., description="Token reCAPTCHA v3 resuelto por el usuario")


class LoginResponse(BaseModel):
    """Captcha login response."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    lawyer: "LawyerInfo"


class LawyerInfo(BaseModel):
    """Basic lawyer info."""
    rut: str
    name: Optional[str] = None
    email: Optional[str] = None


class SessionStatus(BaseModel):
    """Session status response."""
    active: bool
    expires_at: Optional[datetime] = None
    minutes_remaining: Optional[int] = None
    needs_refresh: bool = False
    auth_method: Optional[str] = None


class ClaveUnicaLoginRequest(BaseModel):
    """Clave Única login request."""
    rut: str = Field(..., description="RUT Clave Unica (ej: 16021492-9)")
    password: str = Field(..., description="Clave Unica password")


class ClaveUnicaLoginResponse(BaseModel):
    """Clave Única login response."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    session_id: str
    auth_method: str = "clave_unica"
    lawyer: LawyerInfo


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    """
    Login con credenciales PJUD (captcha).

    Calls ``login_with_token`` on the scraper (ADR-4), resolves (or creates)
    the lawyer record in the DB, persists the session bound to the real
    lawyer_id in Redis via the async store, and returns a signed JWT.
    """
    scraper = PJUDCivilScraper()

    try:
        await scraper.start()

        # AUTH-01: call login_with_token (not login_with_user_captcha)
        pjud_session = await scraper.login_with_token(
            rut=request.rut,
            password=request.password,
            captcha_token=request.captcha_token,
        )

        # Resolve (or create) the lawyer and bind the real id to the session
        lawyer = _get_or_create_lawyer(db, rut=request.rut, auth_method="captcha")
        pjud_session.lawyer_id = lawyer.id

        # Persist session via async store
        store = get_session_store()
        await store.asave_session(pjud_session)

        access_token = create_access_token(
            data={"sub": request.rut},
            expires_delta=timedelta(hours=24),
        )

        return LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,
            lawyer=LawyerInfo(
                rut=request.rut,
                name=lawyer.name,
                email=lawyer.email,
            ),
        )

    except LoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Login PJUD fallido: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno: {exc}",
        )
    finally:
        await scraper.stop()


@router.post("/login/clave-unica", response_model=ClaveUnicaLoginResponse)
async def login_clave_unica(
    request: ClaveUnicaLoginRequest,
    db: Session = Depends(get_db),
):
    """
    Login via Clave Única (Chilean digital identity).

    No captcha required — uses Clave Única credentials directly.
    Resolves (or creates) the lawyer record, then persists the session
    bound to the real lawyer_id in Redis via the async store.
    """
    credentials = ClaveUnicaCredentials(rut=request.rut, password=request.password)

    # Resolve (or create) the lawyer before starting the browser
    lawyer = _get_or_create_lawyer(db, rut=request.rut, auth_method="clave_unica")

    try:
        async with BrowserFactory(headless=True) as factory:
            page = await factory.new_page()

            auth = ClaveUnicaAuth()
            pjud_session = await auth.login(page, credentials, lawyer.id)

            # Persist session
            store = get_session_store()
            await store.asave_session(pjud_session)

            access_token = create_access_token(
                data={"sub": request.rut},
                expires_delta=timedelta(hours=24),
            )

            logger.info("Clave Única login successful for %s", request.rut)

            return ClaveUnicaLoginResponse(
                access_token=access_token,
                token_type="bearer",
                expires_in=24 * 60 * 60,
                session_id=pjud_session.session_id,
                auth_method="clave_unica",
                lawyer=LawyerInfo(
                    rut=request.rut,
                    name=lawyer.name,
                    email=lawyer.email,
                ),
            )

    except ClaveUnicaAuthError as exc:
        logger.warning("Clave Única login failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Clave Unica login failed: {exc}",
        )
    except Exception as exc:
        logger.error("Unexpected error in Clave Única login: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {exc}",
        )


# NOTE: /refresh endpoint is intentionally removed (ADR-4 / AUTH-03).
# Clients must call /login again to re-authenticate.


@router.get("/session-status", response_model=SessionStatus)
async def get_session_status(
    current_rut: str = Depends(get_current_lawyer),
):
    """
    Verify PJUD session status.

    Looks up the session by RUT using the rut secondary index in Redis.
    """
    store = get_session_store()
    session = await store.aget_session_by_rut(current_rut)

    if not session or session.is_expired():
        return SessionStatus(active=False, needs_refresh=True)

    time_left = session.time_until_expiry()
    minutes_remaining = int(time_left.total_seconds() / 60)

    return SessionStatus(
        active=True,
        expires_at=session.expires_at,
        minutes_remaining=minutes_remaining,
        needs_refresh=minutes_remaining < 5,
        auth_method=session.auth_method,
    )


@router.post("/logout")
async def logout(current_rut: str = Depends(get_current_lawyer)):
    """
    Close PJUD session.

    Deletes the session from Redis using the rut secondary index.
    """
    store = get_session_store()
    await store.adelete_session_by_rut(current_rut)
    return {"message": "Sesión cerrada"}


# ============================================================================
# HELPERS
# ============================================================================

def _get_or_create_lawyer(
    db: Session,
    rut: str,
    auth_method: str = "captcha",
) -> Lawyer:
    """Look up a Lawyer by normalized RUT, creating one if absent (ADR-5).

    The operation is idempotent: concurrent inserts are handled by catching
    IntegrityError and re-querying the row that won the race.

    Identity-only (Slice 2): stores rut, name placeholder, and last_login_at.
    Slice 3 will extend this function to populate encrypted credential columns.

    Args:
        db: SQLAlchemy session (sync).
        rut: Lawyer RUT in any input format — normalized internally.
        auth_method: "captcha" or "clave_unica"; stored as preferred_auth_method
                     when creating a new record.

    Returns:
        Persisted Lawyer instance with a valid integer id.  Never returns None.
    """
    normalized = normalize_rut(rut)

    lawyer = db.query(Lawyer).filter(Lawyer.rut == normalized).first()
    if lawyer is None:
        lawyer = Lawyer(
            rut=normalized,
            name=normalized,  # placeholder; Slice 3 / profile sync will update
            preferred_auth_method=auth_method,
            is_active=True,
        )
        db.add(lawyer)
        try:
            db.commit()
            db.refresh(lawyer)
        except IntegrityError:
            # Another request won the INSERT race — roll back and fetch the winner.
            db.rollback()
            lawyer = db.query(Lawyer).filter(Lawyer.rut == normalized).first()

    # Update last_login_at on every successful authentication
    lawyer.last_login_at = datetime.now(tz=timezone.utc)
    db.commit()
    db.refresh(lawyer)

    return lawyer
