"""
PJUD Civil Scraper API endpoints.

Direct endpoints to test the scraper functionality.
Includes resilience (circuit breaker, rate limiting) and observability (metrics, logging).
"""

import time
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

from app.scrapper.pjud.resilience.integration import (
    get_competency_circuit_breaker,
    record_scrape_success,
    record_scrape_error,
)
from app.scrapper.pjud.resilience import CircuitState
from app.scrapper.pjud.observability import get_logger, get_metrics
from app.scrapper.pjud.exceptions import CircuitOpenError

router = APIRouter()

# Initialize logger and metrics
_logger = get_logger("pjud.api")
_metrics = get_metrics()


# ============================================================================
# SCHEMAS
# ============================================================================

class LoginRequest(BaseModel):
    rut: str = Field(..., example="12345678-9")
    password: str = Field(..., example="password123")
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
# In-memory session store (for testing - use Redis in production)
# ============================================================================

_sessions = {}


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

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """
    Login to PJUD with RUT, password and captcha token.
    
    The captcha token must be obtained from the frontend reCAPTCHA widget.
    Returns a session that can be used for subsequent requests.
    """
    scraper = get_scraper()
    
    try:
        session = await scraper.login_with_token(
            rut=request.rut,
            password=request.password,
            captcha_token=request.captcha_token,
        )
        
        # Store session
        session_id = f"pjud_{request.rut}_{datetime.now().timestamp()}"
        _sessions[session_id] = {
            "session": session,
            "scraper": scraper,
            "created_at": datetime.now(),
        }
        
        return LoginResponse(
            success=True,
            rut=session.rut,
            session_id=session_id,
            expires_at=session.expires_at,
            message="Login successful",
        )
        
    except Exception as e:
        await scraper.close()
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/cases/count", response_model=CountResponse)
async def get_cases_count(
    session_id: str = Query(..., description="Session ID from login"),
    year: Optional[str] = Query(None, description="Filter by year (e.g., 2026)"),
):
    """
    Get total count of civil cases without fetching all data.
    Useful for showing totals and progress bars.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    scraper = session_data["scraper"]
    session = session_data["session"]
    
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
    
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("civil")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("civil", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    scraper = session_data["scraper"]
    session = session_data["session"]
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for civil", extra={"year": year, "max_pages": max_pages})
    
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
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    scraper = session_data["scraper"]
    session = session_data["session"]
    
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
    """
    from fastapi.responses import Response
    from app.scrapper.pjud_civil import PJUDDocument
    
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    scraper = session_data["scraper"]
    session = session_data["session"]
    
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
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/logout")
async def logout(
    session_id: str = Query(..., description="Session ID to close"),
):
    """
    Close a PJUD session and cleanup resources.
    """
    if session_id not in _sessions:
        return {"success": True, "message": "Session already closed"}
    
    session_data = _sessions.pop(session_id)
    scraper = session_data["scraper"]
    
    try:
        await scraper.close()
    except:
        pass
    
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
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("laboral")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("laboral", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    session = session_data["session"]
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for laboral", extra={"year": year, "max_pages": max_pages})
    
    # Create laboral scraper
    scraper = get_scraper("laboral")
    
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
    finally:
        await scraper.close()


@router.get("/laboral/cases/{rol}", response_model=CaseDetailResponse)
async def get_laboral_case_detail(
    rol: str,
    session_id: str = Query(..., description="Session ID from login"),
):
    """
    Get full detail of a specific laboral case including movements and documents.
    
    The `rol` must match exactly (e.g., T-123-2026).
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    session = session_data["session"]
    
    scraper = get_scraper("laboral")
    
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await scraper.close()


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
    Includes resilience: circuit breaker, metrics tracking.
    """
    # Check circuit breaker
    cb = get_competency_circuit_breaker("penal")
    if cb.state == CircuitState.OPEN:
        _metrics.record_error("penal", "circuit_rejected")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (circuit open)")
    
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    session = session_data["session"]
    
    start_time = time.time()
    _logger.info(f"Starting get_cases for penal", extra={"year": year, "max_pages": max_pages})
    
    # Create penal scraper
    scraper = get_scraper("penal")
    
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
    finally:
        await scraper.close()


@router.get("/penal/cases/{rol:path}", response_model=CaseDetailResponse)
async def get_penal_case_detail(
    rol: str,
    session_id: str = Query(..., description="Session ID from login"),
):
    """
    Get full detail of a specific penal case including movements and documents.
    
    The `rol` can be a ROL, RUC, or RIT identifier (e.g., O-123-2026, RUC-1234567-8).
    Note: Uses path parameter to allow slashes in RUC format.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    
    session_data = _sessions[session_id]
    session = session_data["session"]
    
    scraper = get_scraper("penal")
    
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await scraper.close()


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
