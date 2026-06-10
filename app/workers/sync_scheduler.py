"""
Sync Scheduler - Background worker for automatic PJUD synchronization.

Uses APScheduler to run sync jobs every N hours for all active lawyers.

Usage:
    # Run as standalone process
    python -m app.workers.sync_scheduler

    # Or import and start in FastAPI lifespan
    from app.workers.sync_scheduler import start_scheduler, stop_scheduler
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.core.database import SessionLocal
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.services.sync_service import SyncService, convert_api_cases_to_scraped
from app.services.session_store import get_session_store
from app.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sync_scheduler")

# Scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None


# ============================================================================
# CONFIGURATION
# ============================================================================

# How often to run sync (in hours)
SYNC_INTERVAL_HOURS = int(getattr(settings, "SYNC_INTERVAL_HOURS", 4))

# Max age before considering data stale (in hours)
MAX_DATA_AGE_HOURS = int(getattr(settings, "MAX_DATA_AGE_HOURS", 4))

# Competencias to sync
COMPETENCIAS = ["civil", "laboral", "penal"]


# ============================================================================
# SYNC JOBS
# ============================================================================

async def sync_lawyer_cases(
    lawyer_id: int,
    competencia: str,
    db: Session,
) -> dict:
    """
    Sync cases for a single lawyer and competencia.
    
    Note: This requires the lawyer to have stored PJUD credentials,
    OR an active session. For now, we'll skip lawyers without sessions.
    
    Returns:
        Dict with sync results
    """
    from app.api.v1.pjud import get_scraper
    
    logger.info(f"Syncing {competencia} for lawyer {lawyer_id}")
    
    # Get active session from Redis store (async)
    store = get_session_store()
    pjud_session = await store.get_session_by_lawyer(lawyer_id)
    
    if not pjud_session:
        logger.warning(f"No active PJUD session for lawyer {lawyer_id}, skipping")
        return {"skipped": True, "reason": "no_session"}
    
    try:
        scraper = get_scraper(competencia)
        session = pjud_session  # PJUDSession object from Redis
        
        # Scrape cases
        cases = await scraper.get_my_cases(
            session=session,
            year="",  # All years
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
            lawyer_id=lawyer_id,
            scraped_cases=scraped_cases,
            competencia=competencia,
            triggered_by="scheduled",
        )
        
        logger.info(
            f"Synced {competencia} for lawyer {lawyer_id}: "
            f"{result.cases_total} cases, {result.cases_new} new"
        )
        
        return {
            "success": True,
            "cases_total": result.cases_total,
            "cases_new": result.cases_new,
        }
        
    except Exception as e:
        logger.error(f"Failed to sync {competencia} for lawyer {lawyer_id}: {e}")
        
        # Record failed sync
        sync_record = SyncHistory(
            lawyer_id=lawyer_id,
            competencia=competencia,
            started_at=datetime.utcnow(),
            triggered_by="scheduled",
        )
        sync_record.complete(status="failed", error=str(e))
        db.add(sync_record)
        db.commit()
        
        return {"success": False, "error": str(e)}
    
    finally:
        if 'scraper' in locals():
            await scraper.close()


async def sync_all_lawyers():
    """
    Sync all active lawyers for all competencias.
    
    This is the main scheduled job that runs every N hours.
    """
    logger.info("=" * 60)
    logger.info("Starting scheduled sync for all lawyers")
    logger.info("=" * 60)
    
    db = SessionLocal()
    
    try:
        # Get all active lawyers
        lawyers = db.query(Lawyer).filter(Lawyer.is_active == True).all()
        
        if not lawyers:
            logger.info("No active lawyers found, nothing to sync")
            return
        
        logger.info(f"Found {len(lawyers)} active lawyers")
        
        results = {
            "total_lawyers": len(lawyers),
            "synced": 0,
            "skipped": 0,
            "failed": 0,
        }
        
        for lawyer in lawyers:
            for competencia in COMPETENCIAS:
                # Check if sync is needed
                sync_service = SyncService(db)
                if not sync_service.needs_sync(lawyer.id, competencia, MAX_DATA_AGE_HOURS):
                    logger.debug(f"Lawyer {lawyer.id} {competencia} is fresh, skipping")
                    continue
                
                result = await sync_lawyer_cases(lawyer.id, competencia, db)
                
                if result.get("skipped"):
                    results["skipped"] += 1
                elif result.get("success"):
                    results["synced"] += 1
                else:
                    results["failed"] += 1
                
                # Small delay between requests to avoid overwhelming PJUD
                await asyncio.sleep(2)
        
        logger.info("=" * 60)
        logger.info(f"Sync complete: {results}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Sync job failed: {e}")
    
    finally:
        db.close()


async def sync_single_lawyer(lawyer_id: int):
    """
    Sync a single lawyer for all competencias.
    
    Can be triggered manually or by webhook.
    """
    logger.info(f"Starting manual sync for lawyer {lawyer_id}")
    
    db = SessionLocal()
    
    try:
        for competencia in COMPETENCIAS:
            await sync_lawyer_cases(lawyer_id, competencia, db)
            await asyncio.sleep(1)
    
    finally:
        db.close()


# ============================================================================
# SCHEDULER MANAGEMENT
# ============================================================================

def get_scheduler() -> AsyncIOScheduler:
    """Get or create the scheduler instance."""
    global _scheduler
    
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    
    return _scheduler


def start_scheduler():
    """
    Start the background scheduler.
    
    Call this from FastAPI lifespan or as standalone process.
    """
    scheduler = get_scheduler()
    
    if scheduler.running:
        logger.warning("Scheduler is already running")
        return
    
    # Add the main sync job
    scheduler.add_job(
        sync_all_lawyers,
        trigger=IntervalTrigger(hours=SYNC_INTERVAL_HOURS),
        id="sync_all_lawyers",
        name="Sync all lawyers every N hours",
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(minutes=5),  # First run in 5 min
    )
    
    # Alternative: Use cron for specific times (e.g., 6am, 12pm, 6pm, 12am)
    # scheduler.add_job(
    #     sync_all_lawyers,
    #     trigger=CronTrigger(hour="0,6,12,18"),
    #     id="sync_all_lawyers_cron",
    #     name="Sync at specific hours",
    #     replace_existing=True,
    # )
    
    scheduler.start()
    logger.info(f"Scheduler started. Sync interval: {SYNC_INTERVAL_HOURS} hours")


def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler
    
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def is_scheduler_running() -> bool:
    """Check if scheduler is running."""
    return _scheduler is not None and _scheduler.running


# ============================================================================
# STANDALONE ENTRY POINT
# ============================================================================

async def main():
    """Run scheduler as standalone process."""
    logger.info("Starting sync scheduler as standalone process")
    
    start_scheduler()
    
    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    finally:
        stop_scheduler()


if __name__ == "__main__":
    asyncio.run(main())
