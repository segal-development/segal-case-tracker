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
from app.core.security import decrypt_pjud_password
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.services.sync_service import (
    SyncService,
    convert_api_cases_to_scraped,
    detect_and_sync_movements,
    _select_cases_for_detail_rotation,
)
from app.services.session_store import get_session_store, SessionStore
from app.services.pjud_session import PJUDSession
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

async def _reauth(
    lawyer: Lawyer,
    store: SessionStore,
) -> tuple[Optional[PJUDSession], Optional[str]]:
    """Attempt autonomous re-authentication for a lawyer whose session has expired.

    SECURITY NOTE (ADR-6 / R1):
    This function decrypts stored PJUD credentials using Fernet symmetric
    encryption.  The stored values are REVERSIBLE ciphertext — NOT hashes.
    Reversibility is required because the worker must replay the plaintext
    password to PJUD during autonomous login.

    What is stored and where:
    - ``lawyers.encrypted_pjud_password``: Fernet ciphertext of the PJUD captcha
      password (captcha auth method).
    - ``lawyers.encrypted_clave_unica_password``: Fernet ciphertext of the Clave
      Única password (clave_unica auth method).

    Key handling requirements:
    - ``settings.ENCRYPTION_KEY`` MUST be sourced from a secret manager
      (never committed to the repository).
    - Key access MUST be restricted to the worker/API roles only.
    - The key MUST be rotated on exposure; rotate by re-encrypting all rows.
    - Plaintext passwords are NEVER written to any log, DB field, or cache
      other than the Fernet ciphertext columns above.

    Branch logic (ADR-7):
    - ``clave_unica``: always re-authenticatable (no captcha).  Decrypts
      ``encrypted_clave_unica_password``, calls headless ``ClaveUnicaAuth.login``,
      persists the fresh session.
    - ``captcha`` + ``settings.has_2captcha``: solves the captcha via 2Captcha
      API, calls ``login_with_token``, persists the fresh session.
    - ``captcha`` + not ``settings.has_2captcha``: returns
      ``(None, "captcha_no_2captcha_key")`` — skips gracefully, does NOT crash.
    - Missing/unrecognised method or no creds: returns
      ``(None, "no_credentials")`` — skips gracefully.

    Args:
        lawyer: Lawyer ORM row with optional encrypted credential fields.
        store: Active ``SessionStore`` for persisting the refreshed session.

    Returns:
        ``(PJUDSession, None)`` on success, or ``(None, reason_str)`` on skip.
    """
    method = getattr(lawyer, "preferred_auth_method", None)

    if method == "clave_unica":
        enc_pass = getattr(lawyer, "encrypted_clave_unica_password", None)
        if not enc_pass:
            logger.warning("Lawyer %d: no encrypted clave_unica password stored", lawyer.id)
            return None, "no_credentials"

        try:
            password = decrypt_pjud_password(str(enc_pass))
        except Exception as exc:
            logger.error("Lawyer %d: failed to decrypt stored credential: %s", lawyer.id, exc)
            return None, "decrypt_failed"

        clave_rut = str(getattr(lawyer, "clave_unica_rut", None) or lawyer.rut)

        # Lazy imports keep Playwright out of the module-level namespace so the
        # worker process doesn't require a browser installed just to import this
        # module.
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        from app.scrapper.pjud.browser import BrowserFactory

        try:
            async with BrowserFactory(headless=True) as factory:
                page = await factory.new_page()
                credentials = ClaveUnicaCredentials(rut=clave_rut, password=password)
                auth = ClaveUnicaAuth()
                session = await auth.login(page, credentials, int(lawyer.id))
                await store.asave_session(session)
                logger.info("Re-auth (clave_unica) succeeded for lawyer %d", lawyer.id)
                return session, None
        except Exception as exc:
            logger.error("Re-auth (clave_unica) failed for lawyer %d: %s", lawyer.id, exc)
            return None, f"reauth_failed: {exc}"

    elif method == "captcha":
        enc_pass = getattr(lawyer, "encrypted_pjud_password", None)
        if not enc_pass:
            logger.warning("Lawyer %d: no encrypted PJUD password stored", lawyer.id)
            return None, "no_credentials"

        if not settings.has_2captcha:
            logger.info(
                "Skipping lawyer %d: captcha re-auth requires a 2Captcha key "
                "(CAPTCHA_API_KEY is not configured)",
                lawyer.id,
            )
            return None, "captcha_no_2captcha_key"

        try:
            password = decrypt_pjud_password(str(enc_pass))
        except Exception as exc:
            logger.error("Lawyer %d: failed to decrypt stored credential: %s", lawyer.id, exc)
            return None, "decrypt_failed"

        from app.scrapper.captcha_solver import CaptchaSolver
        from app.scrapper.pjud_civil import PJUDCivilScraper, RECAPTCHA_SITEKEY

        try:
            solver = CaptchaSolver()
            captcha_token = await solver.solve_recaptcha_v3(
                sitekey=RECAPTCHA_SITEKEY,
                page_url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
                action="validate_captcha_seg_clave_hn",
            )

            scraper = PJUDCivilScraper()
            try:
                await scraper.start()
                session = await scraper.login_with_token(
                    rut=str(lawyer.rut),
                    password=password,
                    captcha_token=captcha_token,
                )
                session.lawyer_id = int(lawyer.id)
                await store.asave_session(session)
                logger.info("Re-auth (captcha+2captcha) succeeded for lawyer %d", lawyer.id)
                return session, None
            finally:
                await scraper.stop()
        except Exception as exc:
            logger.error("Re-auth (captcha) failed for lawyer %d: %s", lawyer.id, exc)
            return None, f"reauth_failed: {exc}"

    else:
        logger.warning(
            "Lawyer %d: preferred_auth_method=%r — no usable stored credentials",
            lawyer.id,
            method,
        )
        return None, "no_credentials"


async def sync_lawyer_cases(
    lawyer_id: int,
    competencia: str,
    db: Session,
) -> dict:
    """
    Sync cases for a single lawyer and competencia.

    When no active session exists, attempts autonomous re-authentication via
    stored credentials (_reauth).  If re-auth is not possible (no creds, no
    2Captcha key) the lawyer is skipped with a descriptive reason rather than
    crashing the scheduler.

    Returns:
        Dict with sync results — one of:
        ``{"skipped": True, "reason": str}`` or
        ``{"success": True, "cases_total": int, "cases_new": int}`` or
        ``{"success": False, "error": str}``.
    """
    from app.api.v1.pjud import get_scraper

    logger.info(f"Syncing {competencia} for lawyer {lawyer_id}")

    # Fetch the lawyer row so _reauth can access stored credentials.
    lawyer = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()

    # Get active session from Redis store (async)
    store = get_session_store()
    pjud_session = await store.get_session_by_lawyer(lawyer_id)

    if pjud_session is None:
        if lawyer is None:
            logger.warning(f"No lawyer found for id {lawyer_id}, skipping")
            return {"skipped": True, "reason": "no_session"}

        pjud_session, reason = await _reauth(lawyer, store)
        if pjud_session is None:
            logger.warning(f"Skipping lawyer {lawyer_id}: {reason}")
            return {"skipped": True, "reason": reason}

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

        # Sync case list to database
        sync_service = SyncService(db)
        result = sync_service.sync_cases(
            lawyer_id=lawyer_id,
            scraped_cases=scraped_cases,
            competencia=competencia,
            triggered_by="scheduled",
        )

        # Select the rotation batch AFTER the full case-list upsert so the DB
        # reflects the most recent state (new cases will have last_detail_checked_at=NULL
        # and therefore be at the front of the queue — highest priority).
        rotation_batch = _select_cases_for_detail_rotation(
            db=db,
            lawyer_id=lawyer_id,
            competencia=competencia,
            api_cases=cases,
            batch_size=settings.DETAIL_BATCH_SIZE,
        )

        # Slice 2: inject a reauth callback so the service can recover from
        # mid-batch session expiry without coupling to the scheduler auth logic.
        async def _reauth_for_lawyer() -> Optional[PJUDSession]:
            if lawyer is None:
                logger.warning(
                    "sync_lawyer_cases: cannot reauth — lawyer_id=%s not found in DB",
                    lawyer_id,
                )
                return None
            new_session, _reason = await _reauth(lawyer, store)
            return new_session

        # S4-T3/S4-T5: movement detection — reuse the shared implementation.
        # selected_cases drives the rotation (replaces the old front-of-list [:5] cap).
        # DETAIL_FETCH_DELAY throttles requests so the worker does not hammer PJUD.
        movements_new, alerts_created, mov_errors = await detect_and_sync_movements(
            db=db,
            scraper=scraper,
            pjud_session=session,
            lawyer_id=lawyer_id,
            api_cases=cases,
            selected_cases=rotation_batch,
            delay_between_fetches=settings.DETAIL_FETCH_DELAY,
            reauth_callback=_reauth_for_lawyer,
        )

        if mov_errors:
            for err in mov_errors:
                logger.warning("sync_lawyer_cases movement error: %s", err)

        # Update the SyncHistory record created by sync_cases with movements_new.
        if movements_new > 0:
            last_sync_history = (
                db.query(SyncHistory)
                .filter(
                    SyncHistory.lawyer_id == lawyer_id,
                    SyncHistory.competencia == competencia,
                )
                .order_by(SyncHistory.started_at.desc())
                .first()
            )
            if last_sync_history:
                last_sync_history.movements_new = movements_new  # type: ignore[assignment]
                last_sync_history.alerts_created = alerts_created  # type: ignore[assignment]
                db.commit()

        logger.info(
            "Synced %s for lawyer %d: %d cases, %d new, %d new movements",
            competencia,
            lawyer_id,
            result.cases_total,
            result.cases_new,
            movements_new,
        )

        return {
            "success": True,
            "cases_total": result.cases_total,
            "cases_new": result.cases_new,
            "movements_new": movements_new,
        }

    except Exception as e:
        logger.error(f"Failed to sync {competencia} for lawyer {lawyer_id}: {e}")

        # Record failed sync (S3-T8: explicitly set cases_found=0 so the column
        # is never NULL/unset on a failure path).
        sync_record = SyncHistory(
            lawyer_id=lawyer_id,
            competencia=competencia,
            started_at=datetime.utcnow(),
            triggered_by="scheduled",
        )
        sync_record.cases_found = 0  # type: ignore[assignment]  # explicit — never unset on failure
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
