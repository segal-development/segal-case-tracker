"""
Authentication endpoints.

El login funciona así:
1. Frontend muestra formulario con RUT + Password + reCAPTCHA de Google
2. Usuario completa el captcha (humano real)
3. Frontend envía RUT + Password + Token del captcha
4. Backend usa esos datos para loguearse en PJUD
5. Backend guarda las cookies de sesión PJUD
6. Backend retorna JWT propio para el frontend
7. Scraping usa las cookies guardadas
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, Field

from app.core.security import create_access_token, get_current_lawyer
from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError
from app.scrapper.session_manager import SessionManager, PJUDSession
from app.models.lawyer import Lawyer


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
    - Decidir cuándo pedir nuevo captcha
    """
    session_manager = SessionManager()
    session = await session_manager.get_session(current_lawyer.rut)
    
    if not session or session.is_expired():
        return SessionStatus(
            active=False,
            needs_refresh=True,
        )
    
    minutes_remaining = int((session.expires_at - datetime.now()).total_seconds() / 60)
    
    return SessionStatus(
        active=True,
        expires_at=session.expires_at,
        minutes_remaining=minutes_remaining,
        needs_refresh=minutes_remaining < 5,  # Pedir refresh si quedan menos de 5 min
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
