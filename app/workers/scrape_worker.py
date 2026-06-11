"""Scrape worker - Process PJUD scraping jobs from Pub/Sub."""

import asyncio
import json
from typing import Callable

from app.config import settings
from app.core.database import SessionLocal
from app.scrapper.pjud_civil import PJUDCivilScraper
from app.services.session_store import get_session_store
from app.services.scrapper_service import ScrapperService


class ScrapeWorker:
    """Process scraping jobs from Pub/Sub queue."""

    async def process_search_job(self, job_data: dict) -> dict:
        """
        Process a search job.

        Args:
            job_data: {job_id, lawyer_id, rol?, rut?, court_id?}

        Returns:
            Results dict with found cases
        """
        job_id = job_data["job_id"]
        lawyer_id = job_data["lawyer_id"]

        db = SessionLocal()
        try:
            service = ScrapperService(db)

            # Update status to processing
            await service.update_job_status(job_id, "processing", progress=10)

            # Get PJUD session from async store
            store = get_session_store()
            session = await store.get_session_by_lawyer(lawyer_id)
            if not session:
                await service.update_job_status(
                    job_id, "failed", error="PJUD session expired"
                )
                return {"error": "PJUD session expired"}
            
            # Scrape
            scraper = PJUDCivilScraper(session_cookies=session)
            
            try:
                if "rol" in job_data:
                    cases = await scraper.search_by_rol(
                        job_data["rol"],
                        job_data.get("court_code"),
                    )
                else:
                    cases = await scraper.search_by_rut(job_data["rut"])
                
                await service.update_job_status(
                    job_id, "completed", progress=100,
                    results={"cases": [c.__dict__ for c in cases]}
                )
                
                return {"cases": cases}
                
            finally:
                await scraper.close()
                
        finally:
            db.close()
    
    async def process_refresh_job(self, job_data: dict) -> dict:
        """
        Process a case refresh job.
        
        Args:
            job_data: {job_id, lawyer_id, case_id}
        
        Returns:
            Results dict with new movements
        """
        # TODO: Implement refresh job processing
        raise NotImplementedError("Refresh job processing not implemented")
    
    def start(self) -> None:
        """
        Start the worker (blocking).
        
        In production, this would subscribe to Pub/Sub.
        """
        # TODO: Implement Pub/Sub subscription
        raise NotImplementedError("Worker startup not implemented")


if __name__ == "__main__":
    worker = ScrapeWorker()
    worker.start()
