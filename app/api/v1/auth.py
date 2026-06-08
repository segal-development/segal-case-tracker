"""
Authentication endpoints.

Two login methods supported:
1. Captcha-based: RUT + Password + reCAPTCHA token (traditional)
2. Clave Unica: RUT + Password via Chilean digital identity (no captcha needed)

Flow for both:
1. Frontend shows appropriate login form
2. Backend authenticates with PJUD
3. Backend stores session cookies in Redis
4. Backend returns JWT for API authentication
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, Field

from app.core.security import create_access_token, get_current_lawyer
from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError
from app.scrapper.session_manager import SessionManager, PJUDSession
from app.scrapper.pjud.browser import BrowserFactory
from app.scrapper.pjud.clave_unica import (
    ClaveUnicaAuth,
    ClaveUnicaCredentials,
    ClaveUnicaAuthError,
)
from app.services.session_store import get_session_store
from app.models.lawyer import Lawyer


logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# SCHEMAS
# ============================================================================

class LoginRequest(BaseModel):
    """Login request from frontend."""
    rut: str = Field(..., description="RUT con dígito verificador (ej: 16021492-9)")
    password: str = Field(..., description="Clave Poder Judicial")
    captcha_token: str = Field(..., description="Token reCAPTCHA v3 resuelto por el usuario")


class LoginResponse(BaseModel):
    """Login response."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos
    lawyer: "LawyerInfo"


class LawyerInfo(BaseModel):
    """Basic lawyer info."""
    rut: str
    name: Optional[str] = None
    email: Optional[str] = None


class RefreshRequest(BaseModel):
    """Refresh session request."""
    captcha_token: str = Field(..., description="Nuevo token reCAPTCHA")


class RefreshResponse(BaseModel):
    """Refresh session response."""
    success: bool
    message: str
    session_expires_at: datetime


class SessionStatus(BaseModel):
    """Session status response."""
    active: bool
    expires_at: Optional[datetime] = None
    minutes_remaining: Optional[int] = None
    needs_refresh: bool = False
    auth_method: Optional[str] = None


class ClaveUnicaLoginRequest(BaseModel):
    """Clave Unica login request."""
    rut: str = Field(..., description="RUT Clave Unica (ej: 16021492-9)")
    password: str = Field(..., description="Clave Unica password")


class ClaveUnicaLoginResponse(BaseModel):
    """Clave Unica login response."""
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
async def login(request: LoginRequest):
    """
    Login con credenciales PJUD.
    
    El frontend debe:
    1. Mostrar formulario con RUT + Password
    2. Incluir reCAPTCHA v3 de Google (sitekey: 6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt)
    3. Enviar el token del captcha resuelto por el usuario
    
    El backend:
    1. Usa las credenciales + token para login en PJUD
    2. Guarda las cookies de sesión PJUD en Redis
    3. Retorna un JWT propio para autenticar requests al API
    """
    session_manager = SessionManager()
    scraper = PJUDCivilScraper(session_manager=session_manager)
    
    try:
        await scraper.start()
        
        # Login en PJUD con el token del captcha que resolvió el usuario
        pjud_session = await scraper.login_with_user_captcha(
            rut=request.rut,
            password=request.password,
            captcha_token=request.captcha_token,
        )
        
        # Crear o actualizar lawyer en DB
        lawyer = await _get_or_create_lawyer(request.rut, request.password)
        
        # Generar JWT propio
        access_token = create_access_token(
            data={"sub": request.rut},
            expires_delta=timedelta(hours=24)
        )
        
        return LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,  # 24 horas
            lawyer=LawyerInfo(
                rut=request.rut,
                name=lawyer.name if lawyer else None,
                email=lawyer.email if lawyer else None,
            )
        )
        
    except LoginError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Login PJUD fallido: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno: {str(e)}"
        )
    finally:
        await scraper.stop()


@router.post("/login/clave-unica", response_model=ClaveUnicaLoginResponse)
async def login_clave_unica(request: ClaveUnicaLoginRequest):
    """
    Login via Clave Unica (Chilean digital identity).
    
    Alternative to captcha-based login. No captcha required - uses
    the user's Clave Unica credentials directly.
    
    Flow:
    1. Frontend collects Clave Unica RUT + password
    2. Backend navigates to PJUD -> Clave Unica portal
    3. Backend fills form and handles redirect
    4. Backend extracts session cookies
    5. Returns JWT for API authentication
    
    Note: Clave Unica credentials may differ from PJUD credentials.
    The RUT used here is the Clave Unica RUT.
    """
    credentials = ClaveUnicaCredentials(
        rut=request.rut,
        password=request.password,
    )
    
    # Get or create lawyer (use RUT as lawyer_id placeholder for now)
    # In production, this would query the database
    lawyer = await _get_or_create_lawyer(request.rut, request.password)
    lawyer_id = lawyer.id if lawyer else 1  # Default to 1 for testing
    
    try:
        async with BrowserFactory(headless=True) as factory:
            page = await factory.new_page()
            
            auth = ClaveUnicaAuth()
            pjud_session = await auth.login(page, credentials, lawyer_id)
            
            # Store session in Redis
            store = get_session_store()
            store.save_session(pjud_session)
            
            # Generate JWT
            access_token = create_access_token(
                data={"sub": request.rut},
                expires_delta=timedelta(hours=24)
            )
            
            logger.info(f"Clave Unica login successful for {request.rut}")
            
            return ClaveUnicaLoginResponse(
                access_token=access_token,
                token_type="bearer",
                expires_in=24 * 60 * 60,
                session_id=pjud_session.session_id,
                auth_method="clave_unica",
                lawyer=LawyerInfo(
                    rut=request.rut,
                    name=lawyer.name if lawyer else None,
                    email=lawyer.email if lawyer else None,
                )
            )
            
    except ClaveUnicaAuthError as e:
        logger.warning(f"Clave Unica login failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Clave Unica login failed: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in Clave Unica login: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(e)}"
        )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_session(
    request: RefreshRequest,
    current_lawyer: Lawyer = Depends(get_current_lawyer)
):
    """
    Refrescar sesión PJUD con nuevo captcha.
    
    Llamar cuando la sesión está por expirar (~20-25 min de inactividad).
    El frontend debe mostrar un reCAPTCHA y enviar el token.
    """
    session_manager = SessionManager()
    scraper = PJUDCivilScraper(session_manager=session_manager)
    
    try:
        await scraper.start()
        
        # Refrescar sesión con nuevo captcha
        new_session = await scraper.refresh_session_with_captcha(
            rut=current_lawyer.rut,
            captcha_token=request.captcha_token,
        )
        
        return RefreshResponse(
            success=True,
            message="Sesión renovada exitosamente",
            session_expires_at=new_session.expires_at,
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error renovando sesión: {str(e)}"
        )
    finally:
        await scraper.stop()


@router.get("/session-status", response_model=SessionStatus)
async def get_session_status(
    current_lawyer: Lawyer = Depends(get_current_lawyer)
):
    """
    Verificar estado de la sesión PJUD.
    
    El frontend puede usar esto para:
    - Mostrar indicador de sesión activa/inactiva
    - Mostrar countdown de expiración
    - Decidir cuándo pedir nuevo captcha (or use Clave Unica)
    - Check which auth method was used
    """
    session_manager = SessionManager()
    session = await session_manager.get_session(current_lawyer.rut)
    
    if not session or session.is_expired():
        return SessionStatus(
            active=False,
            needs_refresh=True,
        )
    
    minutes_remaining = int((session.expires_at - datetime.now()).total_seconds() / 60)
    
    # Get auth_method if available (newer sessions have it)
    auth_method = getattr(session, 'auth_method', 'captcha')
    
    return SessionStatus(
        active=True,
        expires_at=session.expires_at,
        minutes_remaining=minutes_remaining,
        needs_refresh=minutes_remaining < 5,
        auth_method=auth_method,
    )


@router.post("/logout")
async def logout(current_lawyer: Lawyer = Depends(get_current_lawyer)):
    """
    Cerrar sesión.
    
    Invalida la sesión PJUD en cache.
    """
    session_manager = SessionManager()
    await session_manager.invalidate_session(current_lawyer.rut)
    
    return {"message": "Sesión cerrada"}


# ============================================================================
# HELPERS
# ============================================================================

async def _get_or_create_lawyer(rut: str, password: str) -> Optional[Lawyer]:
    """Get or create lawyer in database."""
    # TODO: Implementar con SQLAlchemy
    # Por ahora retorna None
    return None
