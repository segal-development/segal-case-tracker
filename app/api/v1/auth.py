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

from sqlalchemy import func

from app.core.security import (
    create_access_token,
    get_current_lawyer,
    encrypt_pjud_password,
    hash_password,
    verify_password,
)
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


class WebLoginRequest(BaseModel):
    """Email+password login request (app-level auth, not PJUD)."""
    email: str
    password: str


class WebLoginResponse(BaseModel):
    """Email+password login response."""
    access_token: str
    token_type: str = "bearer"


class SetPasswordRequest(BaseModel):
    """Request to set or update a lawyer's app-level password."""
    email: str
    new_password: str = Field(..., min_length=8)


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
        lawyer = _get_or_create_lawyer(
            db, rut=request.rut, password=request.password, auth_method="captcha"
        )
        pjud_session.lawyer_id = int(lawyer.id)

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

    # FIX 2: resolve (or create) the lawyer WITHOUT storing credentials yet.
    # Credentials are persisted only after login succeeds to avoid committing
    # a bad credential when the portal rejects it.
    lawyer = _get_or_create_lawyer(db, rut=request.rut)

    try:
        async with BrowserFactory(headless=True) as factory:
            page = await factory.new_page()

            auth = ClaveUnicaAuth()
            pjud_session = await auth.login(page, credentials, int(lawyer.id))

            # FIX 2: store credentials only AFTER login succeeds.
            _store_encrypted_credentials(db, lawyer, request.password, "clave_unica")

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


@router.post("/web-login", response_model=WebLoginResponse)
def web_login(body: WebLoginRequest, db: Session = Depends(get_db)):
    """
    Login with app-level email + password (bcrypt hash).

    Returns a signed JWT on success.  Always returns 401 with a unified error
    message so the response does not leak which check failed (email unknown vs.
    wrong password vs. no password set).
    """
    lawyer = db.query(Lawyer).filter(
        func.lower(Lawyer.email) == body.email.lower()
    ).first()
    if not lawyer or not lawyer.password_hash or not verify_password(body.password, lawyer.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    token = create_access_token({"sub": lawyer.rut})
    return WebLoginResponse(access_token=token)


@router.put("/password", status_code=200)
def set_password(
    body: SetPasswordRequest,
    db: Session = Depends(get_db),
    current_rut: str = Depends(get_current_lawyer),
):
    """
    Set or update the app-level password for a lawyer account.

    Requires a valid Bearer JWT.  The caller must supply the target email
    alongside the new password (min 8 chars) in the request body.
    """
    # TODO: restrict to admin role once role-based access control is implemented.
    # Currently any authenticated lawyer can change any account's password.
    lawyer = db.query(Lawyer).filter(
        func.lower(Lawyer.email) == body.email.lower()
    ).first()
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    lawyer.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}


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

def _store_encrypted_credentials(
    db: Session,
    lawyer: Lawyer,
    password: str,
    auth_method: str,
) -> None:
    """Encrypt and persist login credentials, updating preferred_auth_method.

    SECURITY NOTE (ADR-6 / R1): stores Fernet-encrypted ciphertext only — the
    plaintext password is NEVER written to any persistent field, log, or cache.
    Call this ONLY after the login attempt has succeeded so a rejected credential
    is never committed to the database.

    FIX 5: ``preferred_auth_method`` is updated symmetrically for both captcha
    and clave_unica so the worker re-auths via the correct branch.
    """
    encrypted = encrypt_pjud_password(password)
    if auth_method == "clave_unica":
        lawyer.clave_unica_rut = str(lawyer.rut)  # type: ignore[assignment]
        lawyer.encrypted_clave_unica_password = encrypted  # type: ignore[assignment]
    else:
        # captcha (default)
        lawyer.encrypted_pjud_password = encrypted  # type: ignore[assignment]
    lawyer.preferred_auth_method = auth_method  # type: ignore[assignment]
    db.commit()
    db.refresh(lawyer)


def _get_or_create_lawyer(
    db: Session,
    rut: str,
    password: Optional[str] = None,
    auth_method: str = "captcha",
) -> Lawyer:
    """Look up a Lawyer by normalized RUT, creating one if absent (ADR-5).

    The operation is idempotent: concurrent inserts are handled by catching
    IntegrityError and re-querying the row that won the race.

    When *password* is provided the encrypted credential is stored via
    ``_store_encrypted_credentials`` (Slice 3 / LID-04).  Callers that need
    post-login credential storage (e.g. clave_unica endpoint) MUST call this
    function WITHOUT a password first, then call ``_store_encrypted_credentials``
    after the login succeeds — never before.

    SECURITY NOTE (ADR-6 / R1):
    - What is stored: Fernet-encrypted ciphertext of the PJUD or Clave Única
      password in ``lawyers.encrypted_pjud_password`` (captcha path) or
      ``lawyers.encrypted_clave_unica_password`` (clave_unica path).
    - This is REVERSIBLE encryption, NOT a hash — the scheduled worker must
      replay the plaintext password to PJUD during autonomous re-authentication.
    - The symmetric key (``settings.ENCRYPTION_KEY``) MUST be sourced from a
      secret manager, restricted to the worker/API roles only, and rotated on
      exposure.  Plaintext passwords are NEVER written to any other persistent
      field, log, or cache.
    - This slice has a mandatory human security-review gate before merge (S3-T9).

    Args:
        db: SQLAlchemy session (sync).
        rut: Lawyer RUT in any input format — normalized internally.
        password: Plaintext credential.  When non-None the encrypted value is
                  stored; when None the credential columns are left unchanged.
        auth_method: ``"captcha"`` or ``"clave_unica"``; stored as
                     ``preferred_auth_method`` when creating a new record.

    Returns:
        Persisted Lawyer instance with a valid integer id.  Never returns None.
    """
    normalized = normalize_rut(rut)

    lawyer = db.query(Lawyer).filter(Lawyer.rut == normalized).first()
    if lawyer is None:
        lawyer = Lawyer(
            rut=normalized,
            name=normalized,  # placeholder; profile sync will update
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
            winner = db.query(Lawyer).filter(Lawyer.rut == normalized).first()
            if winner is None:
                raise RuntimeError(
                    f"Failed to resolve lawyer for RUT {normalized} "
                    "after IntegrityError — concurrent rollback?"
                )
            lawyer = winner

    # Update last_login_at on every successful authentication
    lawyer.last_login_at = datetime.now(tz=timezone.utc)  # type: ignore[assignment]

    # Persist encrypted credentials when provided (Fernet-reversible, see SECURITY NOTE above).
    if password:
        _store_encrypted_credentials(db, lawyer, password, auth_method)
    else:
        db.commit()
        db.refresh(lawyer)

    return lawyer
