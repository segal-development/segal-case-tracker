"""
Browser Factory for fresh browser instances per request.

Creates fresh browser instances per API request to avoid "Target page closed" errors.
Session state is restored from Redis cookies instead of reusing browser instances.

Usage:
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        # Use page for scraping
    # Browser automatically closed

Performance metrics are logged for monitoring browser startup times.
"""

import logging
import time
from typing import Optional, List, Dict, Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext, Playwright

from app.config import settings


logger = logging.getLogger(__name__)


# Performance metrics for monitoring
_browser_startup_times: List[float] = []
_page_creation_times: List[float] = []


def get_browser_metrics() -> Dict[str, Any]:
    """
    Get browser performance metrics.
    
    Returns:
        Dict with startup and page creation timing stats
    """
    def calc_stats(times: List[float]) -> Dict[str, float]:
        if not times:
            return {"count": 0, "avg_ms": 0, "min_ms": 0, "max_ms": 0}
        return {
            "count": len(times),
            "avg_ms": sum(times) / len(times),
            "min_ms": min(times),
            "max_ms": max(times),
        }
    
    return {
        "browser_startup": calc_stats(_browser_startup_times[-100:]),  # Keep last 100
        "page_creation": calc_stats(_page_creation_times[-100:]),
    }


def reset_browser_metrics() -> None:
    """Reset browser metrics (useful for testing)."""
    global _browser_startup_times, _page_creation_times
    _browser_startup_times = []
    _page_creation_times = []


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
        """Start playwright and browser with timing instrumentation."""
        if self._browser is not None:
            logger.debug("Browser already started")
            return
        
        start_time = time.perf_counter()
        logger.info("Starting fresh browser instance")
        
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        
        startup_ms = (time.perf_counter() - start_time) * 1000
        _browser_startup_times.append(startup_ms)
        logger.info(f"Browser started in {startup_ms:.2f}ms")
    
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
        Create page with optional session restoration and timing instrumentation.
        
        Args:
            session: PJUDSession with cookies to restore (optional).
                     Can be from session_store.PJUDSession or session_manager.PJUDSession.
            
        Returns:
            Fresh Page instance with session restored
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Use 'async with BrowserFactory()' context manager.")
        
        start_time = time.perf_counter()
        
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
        
        # Navigate to PJUD index page if session provided (panel loading handled by scraper)
        if session is not None:
            try:
                from app.scrapper.pjud.base import PJUD_INDEX_URL
                
                # Navigate to index page - scraper's _ensure_panel_loaded will handle the rest
                await self._page.goto(PJUD_INDEX_URL, timeout=60000, wait_until="domcontentloaded")
                
                # Restore localStorage if available
                if hasattr(session, 'local_storage') and session.local_storage:
                    try:
                        await self._page.evaluate(
                            f"Object.assign(localStorage, {session.local_storage})"
                        )
                        logger.debug("Restored localStorage from session")
                    except Exception:
                        pass  # localStorage restore is optional
                    
            except Exception as e:
                logger.warning(f"Could not navigate to PJUD: {e}")
        
        creation_ms = (time.perf_counter() - start_time) * 1000
        _page_creation_times.append(creation_ms)
        logger.info(f"Created new page in {creation_ms:.2f}ms (session: {session is not None})")
        return self._page


class WarmBrowserPool:
    """
    Pool of warm browser instances for batch operations.
    
    Keeps 1-2 browsers warm to avoid startup overhead during batch sync.
    NOT for API requests (those use fresh BrowserFactory per request).
    
    Usage (background worker only):
        pool = WarmBrowserPool(size=2)
        await pool.start()
        
        try:
            async with pool.acquire() as factory:
                page = await factory.new_page(session)
                # Use page for scraping
        finally:
            await pool.stop()
    """
    
    DEFAULT_POOL_SIZE = 2
    
    def __init__(self, size: int = DEFAULT_POOL_SIZE, headless: bool = True):
        """
        Initialize warm browser pool.
        
        Args:
            size: Number of browsers to keep warm (default: 2)
            headless: Run browsers in headless mode (default: True)
        """
        self.size = size
        self.headless = headless
        self._pool: List[BrowserFactory] = []
        self._available: List[BrowserFactory] = []
        self._started = False
    
    async def start(self) -> None:
        """Start the pool and warm up browsers."""
        if self._started:
            logger.debug("Pool already started")
            return
        
        logger.info(f"Starting warm browser pool with {self.size} browsers")
        start_time = time.perf_counter()
        
        for i in range(self.size):
            factory = BrowserFactory(headless=self.headless)
            await factory._start()
            self._pool.append(factory)
            self._available.append(factory)
            logger.debug(f"Warmed browser {i + 1}/{self.size}")
        
        startup_ms = (time.perf_counter() - start_time) * 1000
        self._started = True
        logger.info(f"Warm browser pool started in {startup_ms:.2f}ms")
    
    async def stop(self) -> None:
        """Stop all browsers in the pool."""
        if not self._started:
            return
        
        logger.info("Stopping warm browser pool")
        
        for factory in self._pool:
            try:
                await factory._stop()
            except Exception as e:
                logger.debug(f"Error stopping pooled browser: {e}")
        
        self._pool = []
        self._available = []
        self._started = False
        logger.info("Warm browser pool stopped")
    
    def acquire(self) -> "_PooledBrowserContext":
        """
        Acquire a browser from the pool.
        
        Returns:
            Context manager for a pooled browser
        
        Raises:
            RuntimeError: If pool not started or no browsers available
        """
        if not self._started:
            raise RuntimeError("Pool not started. Call await pool.start() first.")
        
        if not self._available:
            raise RuntimeError("No browsers available in pool. Try again later.")
        
        factory = self._available.pop()
        return _PooledBrowserContext(self, factory)
    
    def _release(self, factory: BrowserFactory) -> None:
        """Return a browser to the pool (internal use)."""
        if factory in self._pool and factory not in self._available:
            self._available.append(factory)
            logger.debug(f"Browser returned to pool ({len(self._available)}/{self.size} available)")
    
    @property
    def available_count(self) -> int:
        """Number of available browsers in the pool."""
        return len(self._available)
    
    @property
    def is_started(self) -> bool:
        """Whether the pool is started."""
        return self._started


class _PooledBrowserContext:
    """Context manager for acquiring/releasing pooled browsers."""
    
    def __init__(self, pool: WarmBrowserPool, factory: BrowserFactory):
        self._pool = pool
        self._factory = factory
    
    async def __aenter__(self) -> BrowserFactory:
        """Return the acquired browser factory."""
        return self._factory
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Return browser to pool."""
        self._pool._release(self._factory)
