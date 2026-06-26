"""
PJUD Civil Scraper API endpoints.

Direct endpoints to test the scraper functionality.
Includes resilience (circuit breaker, rate limiting) and observability (metrics, logging).

Architecture:
- Login creates a session stored in Redis (via SessionStore)
- Subsequent requests use BrowserFactory for fresh browser per request
- Session cookies are restored from Redis into each fresh browser
- This eliminates "Target page closed" errors from stale browser refs
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.scrapper.pjud.resilience.integration import (
    get_competency_circuit_breaker,
    record_scrape_success,
    record_scrape_error,
)
from app.scrapper.pjud.resilience import CircuitState
from app.scrapper.pjud.observability import get_logger, get_metrics
from app.scrapper.pjud.exceptions import CircuitOpenError
from app.scrapper.pjud.browser import BrowserFactory
from app.services.session_store import get_session_store
from app.services.pjud_session import PJUDSession
from app.api.deps import get_db, get_current_lawyer
from app.api.v1.auth import _get_or_create_lawyer

router = APIRouter()

# Initialize logger and metrics
_logger = get_logger("pjud.api")
_metrics = get_metrics()


# ============================================================================
# SCHEMAS
# ============================================================================

class LoginRequest(BaseModel):
    rut: str = Field(..., description="RUT con dígito verificador (ej: 12345678-9)")
    password: str = Field(..., description="Clave Poder Judicial")
    captcha_token: str = Field(..., description="reCAPTCHA token from frontend")


class LoginResponse(BaseModel):
    success: bool
    rut: str
    session_id: str
    expires_at: datetime
    message: str


class DocumentSchema(BaseModel):
    token: str
    tipo: str  # "principal" | "anexo"
    url_type: str  # "docuS" | "docuN"


class MovementSchema(BaseModel):
    folio: str
    fecha: str
    tipo_tramite: str
    descripcion: str
    etapa: Optional[str] = None
    foja: Optional[str] = None
    tiene_documento: bool = False
    tiene_anexos: bool = False
    documentos: List[DocumentSchema] = []


class CuadernoSchema(BaseModel):
    token: str
    name: str


class CaseListSchema(BaseModel):
    rol: str
    tribunal: str
    caratulado: str
    fecha_ingreso: str
    estado_cuaderno: Optional[str] = None
    cuaderno: Optional[str] = None
    institucion: Optional[str] = None


class CaseDetailSchema(BaseModel):
    rol: str
    tribunal: str
    caratulado: str
    fecha_ingreso: str
    estado_procesal: Optional[str] = None
    procedimiento: Optional[str] = None
    ubicacion: Optional[str] = None
    etapa: Optional[str] = None
    cuadernos: List[CuadernoSchema] = []
    movements: List[MovementSchema] = []


class CasesListResponse(BaseModel):
    success: bool
    total: int
    filtered_by_year: Optional[str] = None
    cases: List[CaseListSchema]


class CaseDetailResponse(BaseModel):
    success: bool
    case: CaseDetailSchema


class CountResponse(BaseModel):
    success: bool
    year: Optional[str] = None
    total_cases: int
    total_pages: int


# ============================================================================
# Connect schemas
# ============================================================================


class ConnectRequest(BaseModel):
    """Request body for POST /pjud/connect."""

    lawyer_id: int = Field(..., description="DB primary key of the lawyer to connect")
    rut: str = Field(..., description="Lawyer RUT in normalized form (e.g. 12345678-9)")
    auth_method: str = Field(
        ..., description='Authentication method: "segunda_clave" or "clave_unica"'
    )
    captcha_token: Optional[str] = Field(
        None, description="reCAPTCHA v3 token — required when auth_method is segunda_clave"
    )

    @model_validator(mode="after")
    def captcha_required_for_segunda_clave(self) -> "ConnectRequest":
        if self.auth_method == "segunda_clave" and not self.captcha_token:
            raise ValueError("captcha_token is required when auth_method is segunda_clave")
        return self


class ConnectResponse(BaseModel):
    """Response body for POST /pjud/connect."""

    connection_id: str
    status: str  # always "pending" on creation


# ============================================================================
# Session helpers
# ============================================================================

async def _get_session_from_redis(session_id: str) -> PJUDSession:
    """Retrieve a session from Redis by session_id (async).

    Raises:
        HTTPException 401: If session not found or expired.
    """
    store = get_session_store()
    session = await store.aget_session_by_id(session_id)

    if session is None:
        raise HTTPException(status_code=401, detail="Session not found or expired")

    return session


def get_scraper(competency: str = "civil"):
    """Get or create scraper instance for the specified competency."""
    if competency == "laboral":
        from app.scrapper.pjud import LaboralScraper
        return LaboralScraper(headless=True)
    elif competency == "penal":
        from app.scrapper.pjud import PenalScraper
        return PenalScraper(headless=True)
    else:
        from app.scrapper.pjud_civil import PJUDCivilScraper
        return PJUDCivilScraper(headless=True)


# ============================================================================
# ENDPOINTS
# ============================================================================

# ============================================================================
# PJUD ONE-CLICK CONNECT ENDPOINTS
# ============================================================================


@router.post("/connect", response_model=ConnectResponse)
async def pjud_connect(
    request: ConnectRequest,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Enqueue a PJUD one-click connection request.

    Validates the lawyer and their stored credential, inserts a credential-free
    row into pending_connections with status='pending', and returns the new
    connection_id for polling.

    Errors:
      422 — missing captcha_token for segunda_clave (Pydantic validation)
      404 — lawyer_id not found in the database
      409 — no stored credential for the requested auth_method
    """
    from app.models.lawyer import Lawyer as LawyerModel
    from app.services.connection_queue import enqueue_connection

    # Validate auth_method domain
    if request.auth_method not in {"segunda_clave", "clave_unica"}:
        raise HTTPException(
            status_code=422,
            detail="auth_method must be 'segunda_clave' or 'clave_unica'",
        )

    # Look up lawyer by the id supplied in the body
    lawyer = db.query(LawyerModel).filter(LawyerModel.id == request.lawyer_id).first()
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Lawyer not found")

    # Verify that the required encrypted credential is present
    if request.auth_method == "segunda_clave" and not lawyer.encrypted_pjud_password:
        raise HTTPException(
            status_code=409,
            detail="No stored credential for segunda_clave — enroll your PJUD password first",
        )
    if request.auth_method == "clave_unica" and not lawyer.encrypted_clave_unica_password:
        raise HTTPException(
            status_code=409,
            detail="No stored credential for clave_unica — enroll your CU password first",
        )

    # Insert credential-free row (status='pending' set by enqueue_connection)
    connection_id = enqueue_connection(
        db,
        lawyer_id=lawyer.id,
        rut=request.rut,
        auth_method=request.auth_method,
        captcha_token=request.captcha_token,
    )

    _logger.info(
        "Connection enqueued: %s for lawyer %d (method=%s)",
        connection_id,
        lawyer.id,
        request.auth_method,
    )
    return ConnectResponse(connection_id=connection_id, status="pending")


@router.get("/connect/{connection_id}/status")
async def pjud_connect_status(
    connection_id: str,
    db: Session = Depends(get_db),
):
    """Poll the lifecycle status of a PJUD connection request.

    Returns the current status from the pending_connections table:
      pending     — job queued, not yet picked up
      connecting  — watcher is logging in
      connected   — login succeeded; includes cases_synced count
      failed      — login failed; includes error string

    Raises 404 when the connection_id is unknown.
    """
    from app.services.connection_queue import get_status

    status_data = get_status(db, connection_id)
    if status_data is None:
        raise HTTPException(
            status_code=404,
            detail=f"Connection '{connection_id}' not found",
        )
    return status_data


# ============================================================================
# LEGACY STATEFUL LOGIN ENDPOINT
# ============================================================================


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    """
    Login to PJUD with RUT, password and captcha token.

    The captcha token must be obtained from the frontend reCAPTCHA widget.
    Resolves (or creates) the lawyer record in the DB and binds the real
    lawyer_id to the session before persisting it in Redis.
    Returns a session_id that can be used for subsequent requests.
    """
    scraper = get_scraper()

    try:
        # login_with_token returns a canonical PJUDSession (uuid4 session_id, UTC times)
        pjud_session = await scraper.login_with_token(
            rut=request.rut,
            password=request.password,
            captcha_token=request.captcha_token,
        )

        # Resolve (or create) the lawyer and bind the real id to the session (ADR-5)
        lawyer = _get_or_create_lawyer(
            db, rut=request.rut, password=request.password, auth_method="captcha"
        )
        pjud_session.lawyer_id = int(lawyer.id)

        # Persist session via async store (ADR-3)
        store = get_session_store()
        await store.asave_session(pjud_session)

        _logger.info("Login successful, session stored: %s", pjud_session.session_id)

        return LoginResponse(
            success=True,
            rut=pjud_session.rut,
            session_id=pjud_session.session_id,
            expires_at=pjud_session.expires_at,
            message="Login successful",
        )

    except Exception as e:
        _logger.error("Login failed: %s", e)
        raise HTTPException(status_code=401, detail=str(e))
    finally:
        await scraper.close()


@router.get("/cases/count", response_model=CountResponse)
async def get_cases_count(
    session_id: str = Query(..., description="Session ID from login"),
    year: Optional[str] = Query(None, description="Filter by year (e.g., 2026)"),
):
    """
    Get total count of civil cases without fetching all data.
    Useful for showing totals and progress bars.
    
    Uses fresh browser per request with session restoration from Redis.
    """
    session = await _get_session_from_redis(session_id)
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create scraper with injected page
        scraper = get_scraper("civil")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            total_cases, total_pages = await scraper.get_cases_count(
                session=session,
                year=year or "",
            )
            
            return CountResponse(
                success=True,
                year=year,
                total_cases=total_cases,
                total_pages=total_pages,
            )
            
        except Exception as e:
            _logger.error(f"Failed get_cases_count: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/cases", response_model=CasesListResponse)
async def get_cases(
    session_id: str = Query(..., description="Session ID from login"),
    year: Optional[str] = Query(None, description="Filter by year (e.g., 2026)"),
    max_pages: int = Query(0, description="Max pages to fetch (0 = all)"),
):
    """
    Get list of civil cases.
    
    Use `year` to filter by specific year.
    Use `max_pages` to limit results (useful for testing).
    
    Uses fresh browser per request with session restoration from Redis.
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("civil")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("civil", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    session = await _get_session_from_redis(session_id)
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for civil", extra={"year": year, "max_pages": max_pages})
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create scraper with injected page
        scraper = get_scraper("civil")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            cases = await scraper.get_my_cases(
                session=session,
                year=year or "",
                max_pages=max_pages,
            )
            
            # Record success metrics
            duration = time.time() - start_time
            await cb.record_success()
            record_scrape_success("civil", len(cases))
            _metrics.request_duration.observe(duration, competency="civil", endpoint="get_cases")
            _logger.info(f"Completed get_cases: {len(cases)} cases in {duration:.2f}s")
            
            return CasesListResponse(
                success=True,
                total=len(cases),
                filtered_by_year=year,
                cases=[
                    CaseListSchema(
                        rol=c.rol,
                        tribunal=c.tribunal,
                        caratulado=c.caratulado,
                        fecha_ingreso=c.fecha_ingreso,
                        estado_cuaderno=c.estado_cuaderno,
                        cuaderno=c.cuaderno,
                        institucion=c.institucion,
                    )
                    for c in cases
                ],
            )
            
        except Exception as e:
            # Record failure metrics
            await cb.record_failure()
            record_scrape_error("civil", type(e).__name__)
            _logger.error(f"Failed get_cases: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/cases/{rol}", response_model=CaseDetailResponse)
async def get_case_detail(
    rol: str,
    session_id: str = Query(..., description="Session ID from login"),
):
    """
    Get full detail of a specific case including movements and documents.
    
    The `rol` must match exactly (e.g., C-7616-2026).
    Uses fresh browser per request with session restoration from Redis.
    """
    session = await _get_session_from_redis(session_id)
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create scraper with injected page
        scraper = get_scraper("civil")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            # First find the case to get its token
            cases = await scraper.get_my_cases(session=session, max_pages=20)
            
            target_case = None
            for c in cases:
                if c.rol == rol:
                    target_case = c
                    break
            
            if not target_case:
                raise HTTPException(status_code=404, detail=f"Case {rol} not found")
            
            # Get detail
            detail = await scraper.get_case_detail(session, target_case.case_token)
            
            return CaseDetailResponse(
                success=True,
                case=CaseDetailSchema(
                    rol=detail.case.rol,
                    tribunal=detail.case.tribunal,
                    caratulado=detail.case.caratulado,
                    fecha_ingreso=detail.case.fecha_ingreso,
                    estado_procesal=detail.estado_procesal,
                    procedimiento=detail.procedimiento,
                    ubicacion=detail.ubicacion,
                    etapa=detail.etapa,
                    cuadernos=[
                        CuadernoSchema(token=c["token"], name=c["name"])
                        for c in detail.cuadernos
                    ],
                    movements=[
                        MovementSchema(
                            folio=m.folio,
                            fecha=m.fecha,
                            tipo_tramite=m.tipo_tramite,
                            descripcion=m.descripcion,
                            etapa=m.etapa,
                            foja=m.foja,
                            tiene_documento=m.tiene_documento,
                            tiene_anexos=m.tiene_anexos,
                            documentos=[
                                DocumentSchema(
                                    token=d.token,
                                    tipo=d.tipo,
                                    url_type=d.url_type,
                                )
                                for d in m.documentos
                            ],
                        )
                        for m in detail.movements
                    ],
                ),
            )
            
        except HTTPException:
            raise
        except Exception as e:
            _logger.error(f"Failed get_case_detail: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/documents/download")
async def download_document(
    session_id: str = Query(..., description="Session ID from login"),
    token: str = Query(..., description="Document token from movement"),
    url_type: str = Query("docuS", description="Document type: docuS (resolución) or docuN (escrito)"),
):
    """
    Download a document PDF.
    
    Use the `token` and `url_type` from the movement's `documentos` array.
    Returns the PDF file directly.
    Uses fresh browser per request with session restoration from Redis.
    """
    from fastapi.responses import Response
    from app.scrapper.pjud_civil import PJUDDocument
    
    session = await _get_session_from_redis(session_id)
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create scraper with injected page
        scraper = get_scraper("civil")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            # Create document object
            doc = PJUDDocument(
                token=token,
                tipo="principal",
                url_type=url_type,
            )
            
            # Download PDF
            pdf_bytes = await scraper.download_document(session, doc)
            
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename=documento.pdf"
                }
            )
            
        except Exception as e:
            _logger.error(f"Failed download_document: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.delete("/logout")
async def logout(
    session_id: str = Query(..., description="Session ID to close"),
):
    """
    Close a PJUD session and cleanup resources.
    
    Removes the session from Redis. No browser cleanup needed since
    we use fresh browser per request.
    """
    store = get_session_store()
    await store.adelete_session(session_id)

    _logger.info("Session logged out: %s", session_id)
    return {"success": True, "message": "Session closed"}


# ============================================================================
# LABORAL ENDPOINTS
# ============================================================================

@router.get("/laboral/cases", response_model=CasesListResponse)
async def get_laboral_cases(
    session_id: str = Query(..., description="Session ID from login"),
    year: Optional[str] = Query(None, description="Filter by year (e.g., 2026)"),
    max_pages: int = Query(0, description="Max pages to fetch (0 = all)"),
):
    """
    Get list of laboral cases.
    
    Use `year` to filter by specific year.
    Use `max_pages` to limit results (useful for testing).
    
    Note: Laboral cases have 7 columns (vs Civil's 8) - no Institucion column.
    Uses fresh browser per request with session restoration from Redis.
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("laboral")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("laboral", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    session = await _get_session_from_redis(session_id)
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for laboral", extra={"year": year, "max_pages": max_pages})
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create laboral scraper with injected page
        scraper = get_scraper("laboral")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            cases = await scraper.get_my_cases(
                session=session,
                year=year or "",
                max_pages=max_pages,
            )
            
            # Record success metrics
            duration = time.time() - start_time
            await cb.record_success()
            record_scrape_success("laboral", len(cases))
            _metrics.request_duration.observe(duration, competency="laboral", endpoint="get_cases")
            _logger.info(f"Completed get_cases laboral: {len(cases)} cases in {duration:.2f}s")
            
            return CasesListResponse(
                success=True,
                total=len(cases),
                filtered_by_year=year,
                cases=[
                    CaseListSchema(
                        rol=c.rol,
                        tribunal=c.tribunal,
                        caratulado=c.caratulado,
                        fecha_ingreso=c.fecha_ingreso,
                        estado_cuaderno=c.estado_cuaderno,
                        cuaderno=c.cuaderno,
                        institucion=c.institucion,
                    )
                    for c in cases
                ],
            )
            
        except Exception as e:
            # Record failure metrics
            await cb.record_failure()
            record_scrape_error("laboral", type(e).__name__)
            _logger.error(f"Failed get_cases laboral: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/laboral/cases/{rol}", response_model=CaseDetailResponse)
async def get_laboral_case_detail(
    rol: str,
    session_id: str = Query(..., description="Session ID from login"),
):
    """
    Get full detail of a specific laboral case including movements and documents.
    
    The `rol` must match exactly (e.g., T-123-2026).
    Uses fresh browser per request with session restoration from Redis.
    """
    session = await _get_session_from_redis(session_id)
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create laboral scraper with injected page
        scraper = get_scraper("laboral")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            # First find the case to get its token
            cases = await scraper.get_my_cases(session=session, max_pages=20)
            
            target_case = None
            for c in cases:
                if c.rol == rol:
                    target_case = c
                    break
            
            if not target_case:
                raise HTTPException(status_code=404, detail=f"Laboral case {rol} not found")
            
            # Get detail
            detail = await scraper.get_case_detail(session, target_case.case_token)
            
            return CaseDetailResponse(
                success=True,
                case=CaseDetailSchema(
                    rol=detail.case.rol,
                    tribunal=detail.case.tribunal,
                    caratulado=detail.case.caratulado,
                    fecha_ingreso=detail.case.fecha_ingreso,
                    estado_procesal=detail.estado_procesal,
                    procedimiento=detail.procedimiento,
                    ubicacion=detail.ubicacion,
                    etapa=detail.etapa,
                    cuadernos=[
                        CuadernoSchema(token=c["token"], name=c["name"])
                        for c in detail.cuadernos
                    ],
                    movements=[
                        MovementSchema(
                            folio=m.folio,
                            fecha=m.fecha,
                            tipo_tramite=m.tipo_tramite,
                            descripcion=m.descripcion,
                            etapa=m.etapa,
                            foja=m.foja,
                            tiene_documento=m.tiene_documento,
                            tiene_anexos=m.tiene_anexos,
                            documentos=[
                                DocumentSchema(
                                    token=d.token,
                                    tipo=d.tipo,
                                    url_type=d.url_type,
                                )
                                for d in m.documentos
                            ],
                        )
                        for m in detail.movements
                    ],
                ),
            )
            
        except HTTPException:
            raise
        except Exception as e:
            _logger.error(f"Failed get_laboral_case_detail: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# PENAL ENDPOINTS
# ============================================================================

@router.get("/penal/cases", response_model=CasesListResponse)
async def get_penal_cases(
    session_id: str = Query(..., description="Session ID from login"),
    year: Optional[str] = Query(None, description="Filter by year (e.g., 2026)"),
    max_pages: int = Query(0, description="Max pages to fetch (0 = all)"),
):
    """
    Get list of penal cases.
    
    Use `year` to filter by specific year.
    Use `max_pages` to limit results (useful for testing).
    
    Note: Penal uses RUC (Rol Unico de Causa) instead of ROL in some contexts.
    Uses fresh browser per request with session restoration from Redis.
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("penal")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("penal", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    session = await _get_session_from_redis(session_id)
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for penal", extra={"year": year, "max_pages": max_pages})
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create penal scraper with injected page
        scraper = get_scraper("penal")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            cases = await scraper.get_my_cases(
                session=session,
                year=year or "",
                max_pages=max_pages,
            )
            
            # Record success metrics
            duration = time.time() - start_time
            await cb.record_success()
            record_scrape_success("penal", len(cases))
            _metrics.request_duration.observe(duration, competency="penal", endpoint="get_cases")
            _logger.info(f"Completed get_cases penal: {len(cases)} cases in {duration:.2f}s")
            
            return CasesListResponse(
                success=True,
                total=len(cases),
                filtered_by_year=year,
                cases=[
                    CaseListSchema(
                        rol=c.rol,
                        tribunal=c.tribunal,
                        caratulado=c.caratulado,
                        fecha_ingreso=c.fecha_ingreso,
                        estado_cuaderno=c.estado_cuaderno,
                        cuaderno=c.cuaderno,
                        institucion=c.institucion,
                    )
                    for c in cases
                ],
            )
            
        except Exception as e:
            # Record failure metrics
            await cb.record_failure()
            record_scrape_error("penal", type(e).__name__)
            _logger.error(f"Failed get_cases penal: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/penal/cases/{rol:path}", response_model=CaseDetailResponse)
async def get_penal_case_detail(
    rol: str,
    session_id: str = Query(..., description="Session ID from login"),
):
    """
    Get full detail of a specific penal case including movements and documents.
    
    The `rol` can be a ROL, RUC, or RIT identifier (e.g., O-123-2026, RUC-1234567-8).
    Note: Uses path parameter to allow slashes in RUC format.
    Uses fresh browser per request with session restoration from Redis.
    """
    session = await _get_session_from_redis(session_id)
    
    # Use fresh browser for this request
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        
        # Create penal scraper with injected page
        scraper = get_scraper("penal")
        scraper._page = page
        scraper._browser = factory._browser
        scraper._context = factory._context
        
        try:
            # First find the case to get its token
            cases = await scraper.get_my_cases(session=session, max_pages=20)
            
            target_case = None
            for c in cases:
                if c.rol == rol:
                    target_case = c
                    break
            
            if not target_case:
                raise HTTPException(status_code=404, detail=f"Penal case {rol} not found")
            
            # Get detail
            detail = await scraper.get_case_detail(session, target_case.case_token)
            
            return CaseDetailResponse(
                success=True,
                case=CaseDetailSchema(
                    rol=detail.case.rol,
                    tribunal=detail.case.tribunal,
                    caratulado=detail.case.caratulado,
                    fecha_ingreso=detail.case.fecha_ingreso,
                    estado_procesal=detail.estado_procesal,
                    procedimiento=detail.procedimiento,
                    ubicacion=detail.ubicacion,
                    etapa=detail.etapa,
                    cuadernos=[
                        CuadernoSchema(token=c["token"], name=c["name"])
                        for c in detail.cuadernos
                    ],
                    movements=[
                        MovementSchema(
                            folio=m.folio,
                            fecha=m.fecha,
                            tipo_tramite=m.tipo_tramite,
                            descripcion=m.descripcion,
                            etapa=m.etapa,
                            foja=m.foja,
                            tiene_documento=m.tiene_documento,
                            tiene_anexos=m.tiene_anexos,
                            documentos=[
                                DocumentSchema(
                                    token=d.token,
                                    tipo=d.tipo,
                                    url_type=d.url_type,
                                )
                                for d in m.documentos
                            ],
                        )
                        for m in detail.movements
                    ],
                ),
            )
            
        except HTTPException:
            raise
        except Exception as e:
            _logger.error(f"Failed get_penal_case_detail: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SELECTOR MANAGEMENT
# ============================================================================

class ReloadSelectorsResponse(BaseModel):
    success: bool
    results: dict
    message: str


@router.post("/selectors/reload", response_model=ReloadSelectorsResponse)
async def reload_selectors_endpoint(
    competencia: Optional[str] = Query(None, description="Specific competency to reload (e.g., 'civil'), or omit for all"),
):
    """
    Hot-reload selectors from YAML files.
    
    Use this endpoint when PJUD updates their HTML structure.
    Updates the YAML file and call this endpoint to apply changes
    without restarting the service.
    
    Example:
        POST /api/v1/pjud/selectors/reload?competencia=civil
    """
    from app.scrapper.pjud import reload_selectors
    
    try:
        results = reload_selectors(competencia)
        
        all_success = all(results.values()) if results else False
        
        return ReloadSelectorsResponse(
            success=all_success,
            results=results,
            message=f"Reloaded {len(results)} competencies" if results else "No competencies loaded to reload",
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# HEALTH CHECK
# ============================================================================

class CircuitBreakerStatusSchema(BaseModel):
    name: str
    state: str
    failure_count: int
    time_until_recovery: int


class HealthCheckSchema(BaseModel):
    status: str
    response_time_ms: int
    structure_changed: bool
    current_hash: Optional[str] = None
    baseline_hash: Optional[str] = None
    error: Optional[str] = None
    timestamp: float


class HealthResponse(BaseModel):
    success: bool
    pjud_status: str
    circuit_breaker: Optional[CircuitBreakerStatusSchema] = None
    last_health_check: Optional[HealthCheckSchema] = None
    message: str


@router.get("/health", response_model=HealthResponse)
async def get_pjud_health():
    """
    Get PJUD health status.
    
    Returns:
    - PJUD portal availability status
    - Circuit breaker state
    - Last health check results including structure hash
    
    Use this to monitor PJUD availability before making requests.
    """
    from app.scrapper.pjud.resilience import get_health_checker
    from app.scrapper.pjud.resilience.circuit_breaker import get_circuit_breaker
    
    try:
        # Get health checker
        checker = get_health_checker()
        
        # Perform health check if no recent result
        if checker.last_result is None:
            await checker.check()
        
        last_result = checker.last_result
        
        # Get circuit breaker status
        cb = get_circuit_breaker("pjud-civil")
        cb_status = CircuitBreakerStatusSchema(
            name=cb.name,
            state=cb.state.value,
            failure_count=cb.failure_count,
            time_until_recovery=cb.time_until_recovery,
        )
        
        # Build response
        health_check = None
        if last_result:
            health_check = HealthCheckSchema(
                status=last_result.status.value,
                response_time_ms=last_result.response_time_ms,
                structure_changed=last_result.structure_changed,
                current_hash=last_result.current_hash,
                baseline_hash=last_result.baseline_hash,
                error=last_result.error,
                timestamp=last_result.timestamp,
            )
        
        pjud_status = last_result.status.value if last_result else "unknown"
        
        return HealthResponse(
            success=True,
            pjud_status=pjud_status,
            circuit_breaker=cb_status,
            last_health_check=health_check,
            message="Health check completed",
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/health/baseline", response_model=HealthResponse)
async def update_health_baseline():
    """
    Update the health check baseline hash.
    
    Call this after verifying that a structure change is expected
    (e.g., PJUD updated their UI intentionally).
    
    This resets the structure_changed flag.
    """
    from app.scrapper.pjud.resilience import get_health_checker
    
    try:
        checker = get_health_checker()
        
        # Perform fresh check
        result = await checker.check()
        
        # Update baseline
        checker.update_baseline()
        
        return HealthResponse(
            success=True,
            pjud_status=result.status.value,
            circuit_breaker=None,
            last_health_check=HealthCheckSchema(
                status=result.status.value,
                response_time_ms=result.response_time_ms,
                structure_changed=False,  # Reset after baseline update
                current_hash=result.current_hash,
                baseline_hash=checker.baseline_hash,
                error=result.error,
                timestamp=result.timestamp,
            ),
            message=f"Baseline updated to {checker.baseline_hash}",
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker(
    name: str = Query("pjud-civil", description="Circuit breaker name"),
):
    """
    Manually reset a circuit breaker to closed state.
    
    Use this after PJUD recovers from an outage.
    """
    from app.scrapper.pjud.resilience.circuit_breaker import get_circuit_breaker
    
    try:
        cb = get_circuit_breaker(name)
        await cb.reset()
        
        return {
            "success": True,
            "name": cb.name,
            "state": cb.state.value,
            "message": "Circuit breaker reset to closed",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# METRICS ENDPOINT
# ============================================================================

@router.get("/metrics")
async def get_pjud_metrics():
    """
    Get PJUD metrics in Prometheus text format.
    
    Returns metrics including:
    - pjud_cases_scraped_total: Total cases scraped by competency
    - pjud_requests_total: Total requests by competency and endpoint
    - pjud_errors_total: Total errors by competency and type
    - pjud_request_duration_seconds: Request duration histogram
    - pjud_circuit_state: Circuit breaker state gauge (0=closed, 1=open, 2=half_open)
    
    Example response:
    ```
    # HELP pjud_cases_scraped_total Total number of cases scraped from PJUD
    # TYPE pjud_cases_scraped_total counter
    pjud_cases_scraped_total{competency="civil"} 150
    ```
    """
    from fastapi.responses import PlainTextResponse
    from app.scrapper.pjud.observability import metrics_endpoint
    
    content = metrics_endpoint()
    return PlainTextResponse(content=content, media_type="text/plain")


@router.get("/alerts/history")
async def get_alerts_history(
    limit: int = Query(10, ge=1, le=100, description="Number of recent alerts to return"),
):
    """
    Get recent alert history.
    
    Returns the most recent alerts sent by the system.
    Useful for debugging and monitoring alert delivery.
    """
    from app.scrapper.pjud.observability import get_alert_manager
    
    manager = get_alert_manager()
    return {
        "success": True,
        "alerts": manager.get_history(limit=limit),
        "total": len(manager._history),
    }
