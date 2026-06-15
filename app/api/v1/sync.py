"""
Sync endpoints - Synchronize data from PJUD to database.

POST /sync          - Trigger manual sync
GET  /sync/status   - Get last sync status
"""

import logging
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pydantic import BaseModel

from app.api.deps import get_db, get_current_lawyer
from app.models.sync_history import SyncHistory
from app.models.lawyer import Lawyer
from app.services.sync_service import (
    SyncService,
    SyncResult,
    convert_api_cases_to_scraped,
    detect_and_sync_movements,
)
from app.services.session_store import get_session_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# SCHEMAS
# ============================================================================

class SyncRequest(BaseModel):
    """Request to sync data from PJUD."""
    competencia: str = "civil"  # civil, laboral, penal
    year: Optional[str] = None  # Filter by year
    session_id: str  # PJUD session ID from login
    rol: Optional[str] = None  # If set, only fetch movements for this ROL (demo scoping)


class SyncStatusResponse(BaseModel):
    """Status of last sync."""
    last_sync: Optional[datetime] = None
    status: Optional[str] = None
    cases_found: int = 0
    cases_new: int = 0
    movements_new: int = 0
    duration_seconds: Optional[int] = None
    needs_sync: bool = True
    
    class Config:
        from_attributes = True


class SyncResponse(BaseModel):
    """Result of sync operation."""
    success: bool
    message: str
    cases_total: int = 0
    cases_new: int = 0
    cases_updated: int = 0
    movements_new: int = 0
    alerts_created: int = 0
    duration_seconds: Optional[int] = None
    errors: List[str] = []


class SyncHistoryResponse(BaseModel):
    """Single sync history entry."""
    id: int
    competencia: str
    year: Optional[str]
    status: str
    cases_found: int
    cases_new: int
    movements_new: int
    started_at: datetime
    completed_at: Optional[datetime]
    duration_seconds: Optional[int]
    triggered_by: str


# ============================================================================
# ENDPOINTS
# ============================================================================

def _resolve_lawyer_id(db: Session, current_lawyer: dict) -> int:
    """Resolve the numeric lawyer id from the JWT.

    The JWT ``sub`` is the lawyer RUT (e.g. "16021492-9"); legacy/test tokens
    may carry the numeric id directly. Map the RUT to ``lawyers.id`` via the DB.
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


@router.post("", response_model=SyncResponse)
async def sync_now(
    request: SyncRequest,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    Manually trigger sync from PJUD.
    
    Requires a valid PJUD session_id (from /pjud/login).
    This will:
    1. Scrape cases from PJUD
    2. Save/update cases in database
    3. Detect new movements
    4. Create alerts for changes
    """
    from app.api.v1.pjud import get_scraper
    
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not lawyer_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Get session from Redis store (async)
    store = get_session_store()
    session = await store.aget_session_by_id(request.session_id)

    if not session:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired PJUD session. Please login again at /pjud/login",
        )
    
    # The JWT `sub` is the lawyer RUT (e.g. "16021492-9"), NOT the numeric id.
    # Use the session's bound lawyer_id (set during login) for all DB writes.
    resolved_lawyer_id = session.lawyer_id

    start_time = datetime.utcnow()

    try:
        # Get scraper for competencia
        scraper = get_scraper(request.competencia)
        
        # Scrape cases from PJUD
        cases = await scraper.get_my_cases(
            session=session,
            year=request.year or "",
            max_pages=0,  # All pages
        )
        
        # Convert to ScrapedCase objects
        scraped_cases = convert_api_cases_to_scraped([
            {
                "rol": c.rol,
                "tribunal": c.tribunal,
                "caratulado": c.caratulado,
                "fecha_ingreso": c.fecha_ingreso,
                "estado_cuaderno": c.estado_cuaderno,
                "cuaderno": c.cuaderno,
                "institucion": c.institucion,
            }
            for c in cases
        ])
        
        # Sync to database
        sync_service = SyncService(db)
        result = sync_service.sync_cases(
            lawyer_id=resolved_lawyer_id,
            scraped_cases=scraped_cases,
            competencia=request.competencia,
            year=request.year,
            triggered_by="manual",
        )

        # ------------------------------------------------------------------ #
        # Movement detection — delegate to the shared detect_and_sync_movements
        # helper so the endpoint and the scheduled worker share one code path.
        # Demo scoping: if `rol` is provided → only that case;
        # otherwise → cap at MOVEMENT_CHECK_DEFAULT_MAX cases.
        # ------------------------------------------------------------------ #
        movements_new, alerts_new, mov_errors = await detect_and_sync_movements(
            db=db,
            scraper=scraper,
            pjud_session=session,
            lawyer_id=resolved_lawyer_id,
            api_cases=cases,
            rol=request.rol,
        )
        result.movements_new += movements_new
        result.alerts_created += alerts_new
        result.errors.extend(mov_errors)

        duration = int((datetime.utcnow() - start_time).total_seconds())

        return SyncResponse(
            success=True,
            message=f"Synced {result.cases_total} cases from PJUD",
            cases_total=result.cases_total,
            cases_new=result.cases_new,
            cases_updated=result.cases_updated,
            movements_new=result.movements_new,
            alerts_created=result.alerts_created,
            duration_seconds=duration,
            errors=result.errors,
        )
        
    except Exception as e:
        # Log error in sync history
        sync_record = SyncHistory(
            lawyer_id=resolved_lawyer_id,
            competencia=request.competencia,
            year=request.year,
            started_at=start_time,
            triggered_by="manual",
        )
        sync_record.complete(status="failed", error=str(e))
        db.add(sync_record)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail=f"Sync failed: {str(e)}",
        )
    finally:
        if 'scraper' in locals():
            await scraper.close()


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(
    competencia: str = Query("civil", description="Competencia to check"),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get status of last sync for a competencia."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    sync_service = SyncService(db)
    last_sync = sync_service.get_last_sync(lawyer_id, competencia)
    needs_sync = sync_service.needs_sync(lawyer_id, competencia)
    
    if not last_sync:
        return SyncStatusResponse(
            last_sync=None,
            status=None,
            needs_sync=True,
        )
    
    return SyncStatusResponse(
        last_sync=last_sync.completed_at,
        status=last_sync.status,
        cases_found=last_sync.cases_found,
        cases_new=last_sync.cases_new,
        movements_new=last_sync.movements_new,
        duration_seconds=last_sync.duration_seconds,
        needs_sync=needs_sync,
    )


@router.get("/history", response_model=List[SyncHistoryResponse])
async def get_sync_history(
    competencia: Optional[str] = Query(None, description="Filter by competencia"),
    limit: int = Query(10, ge=1, le=50),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get sync history for the lawyer."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)

    query = db.query(SyncHistory).filter(SyncHistory.lawyer_id == lawyer_id)
    
    if competencia:
        query = query.filter(SyncHistory.competencia == competencia)
    
    history = query.order_by(SyncHistory.started_at.desc()).limit(limit).all()
    
    return [
        SyncHistoryResponse(
            id=h.id,
            competencia=h.competencia,
            year=h.year,
            status=h.status,
            cases_found=h.cases_found or 0,
            cases_new=h.cases_new or 0,
            movements_new=h.movements_new or 0,
            started_at=h.started_at,
            completed_at=h.completed_at,
            duration_seconds=h.duration_seconds,
            triggered_by=h.triggered_by or "manual",
        )
        for h in history
    ]
