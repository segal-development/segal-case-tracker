"""
Browser Factory for fresh browser instances per request.

Creates fresh browser instances per API request to avoid "Target page closed" errors.
Session state is restored from Redis cookies instead of reusing browser instances.

Usage:
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        # Use page for scraping
    # Browser automatically closed
"""

import logging
from typing import Optional, List, Dict, Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext, Playwright

from app.config import settings


logger = logging.getLogger(__name__)


class BrowserFactory:
    """
    Context manager for fresh browser instances.
    
    Creates a new browser on enter, closes it on exit.
    Session state is restored via cookies, not browser reuse.
    
    Usage:
        async with BrowserFactory() as factory:
            page = await factory.new_page(session)
            scraper = CivilScraper(page=page)
            return await scraper.get_my_cases()
    """
    
    def __init__(self, headless: bool = True):
        """
        Initialize BrowserFactory.
        
        Args:
            headless: Run browser in headless mode (default: True)
        """
        self.headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
    
    async def __aenter__(self) -> "BrowserFactory":
        """Start browser and return factory."""
        await self._start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close browser and cleanup."""
        await self._stop()
    
    async def _start(self) -> None:
        """Start playwright and browser."""
        if self._browser is not None:
            logger.debug("Browser already started")
            return
        
        logger.info("Starting fresh browser instance")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        logger.debug("Browser started successfully")
    
    async def _stop(self) -> None:
        """Stop browser and playwright."""
        logger.debug("Stopping browser instance")
        
        # Close page if exists
        if self._page is not None:
            try:
                await self._page.close()
            except Exception as e:
                logger.debug(f"Error closing page: {e}")
            self._page = None
        
        # Close context if exists
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as e:
                logger.debug(f"Error closing context: {e}")
            self._context = None
        
        # Close browser
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.debug(f"Error closing browser: {e}")
            self._browser = None
        
        # Stop playwright
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.debug(f"Error stopping playwright: {e}")
            self._playwright = None
        
        logger.info("Browser instance stopped")
    
    async def new_page(
        self,
        session: Optional[Any] = None,
    ) -> Page:
        """
        Create page with optional session restoration.
        
        Args:
            session: PJUDSession with cookies to restore (optional).
                     Can be from session_store.PJUDSession or session_manager.PJUDSession.
            
        Returns:
            Fresh Page instance with session restored
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Use 'async with BrowserFactory()' context manager.")
        
        # Close existing context if any (clean slate per new_page call)
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
        
        # Create new browser context
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Restore cookies from session if provided
        if session is not None and hasattr(session, 'cookies') and session.cookies:
            logger.debug(f"Restoring {len(session.cookies)} cookies from session")
            await self._context.add_cookies(session.cookies)
        
        # Create new page
        self._page = await self._context.new_page()
        
        # Restore localStorage if session provided
        if session is not None and hasattr(session, 'local_storage') and session.local_storage:
            # Need to navigate to a page first before setting localStorage
            try:
                from app.scrapper.pjud.base import PJUD_HOME_URL
                await self._page.goto(PJUD_HOME_URL, timeout=60000)
                await self._page.evaluate(
                    f"Object.assign(localStorage, {session.local_storage})"
                )
                logger.debug("Restored localStorage from session")
            except Exception as e:
                logger.warning(f"Could not restore localStorage: {e}")
        
        logger.info(f"Created new page with session restoration: {session is not None}")
        return self._page
