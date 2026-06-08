"""Scrapper endpoints - PJUD search and polling."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer
from app.schemas.common import JobResponse, JobStatusResponse

router = APIRouter()


@router.get("/search")
async def search_pjud(
    rol: str | None = Query(None, description="Case ROL (e.g., C-1234-2024)"),
    rut: str | None = Query(None, description="Party RUT"),
    court_id: int | None = Query(None, description="Court ID"),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    Start a PJUD search job.
    
    Returns a job ID for polling the results.
    Search is performed asynchronously via Pub/Sub workers.
    """
    if not rol and not rut:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'rol' or 'rut' must be provided",
        )
    
    # TODO: Create scrape job and publish to Pub/Sub
    
    return JobResponse(
        job_id="job-12345",
        status="pending",
        message="Search job created",
    )


@router.get("/poll/{job_id}", response_model=JobStatusResponse)
async def poll_job(
    job_id: str,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    Poll a search job for results.
    
    Returns:
    - pending: Job is queued
    - processing: Job is being processed
    - completed: Results available
    - failed: Job failed with error
    """
    # TODO: Check job status in Redis
    
    return JobStatusResponse(
        job_id=job_id,
        status="pending",
        progress=0,
        results=None,
        error=None,
    )


@router.post("/refresh/{case_id}")
async def refresh_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    Trigger a refresh of case movements from PJUD.
    
    Returns a job ID for polling.
    """
    # TODO: Create refresh job
    
    return JobResponse(
        job_id="job-67890",
        status="pending",
        message="Refresh job created",
    )
