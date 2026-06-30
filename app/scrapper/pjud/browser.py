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

import json
import logging
import time
from typing import Optional, List, Dict, Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext, Playwright

from app.config import settings

# Copied here to keep browser.py self-contained (no circular-import risk).
_PJUD_BASE_URL = "https://oficinajudicialvirtual.pjud.cl"

# User-agent matching the real system Chrome installed on this Mac (v149).
_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


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


def build_storage_state(session) -> Optional[Dict[str, Any]]:
    """Build a Playwright ``storage_state`` dict from a PJUD session.

    Returning a storage_state with both cookies **and** localStorage means the
    browser context has them available from the very first page load, so PJUD's
    indexN.php can authenticate the request before executing any JavaScript.

    Args:
        session: Any object with ``cookies`` (list) and ``local_storage`` (str)
                 attributes, or ``None``.

    Returns:
        Dict suitable for ``new_context(storage_state=...)``, or ``None`` when
        *session* is ``None``.
    """
    if session is None:
        return None

    cookies = getattr(session, "cookies", None) or []
    local_storage_raw: str = getattr(session, "local_storage", None) or ""

    # Parse localStorage JSON safely — any malformed/empty value → empty list.
    ls_items: List[Dict[str, str]] = []
    if local_storage_raw and local_storage_raw.strip() not in ("", "{}"):
        try:
            parsed = json.loads(local_storage_raw)
            if isinstance(parsed, dict):
                ls_items = [{"name": k, "value": str(v)} for k, v in parsed.items()]
        except Exception:
            pass  # Graceful fallback: proceed with empty localStorage

    return {
        "cookies": cookies,
        "origins": [
            {
                "origin": _PJUD_BASE_URL,
                "localStorage": ls_items,
            }
        ],
    }


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
        """Start playwright and browser with timing instrumentation.

        Uses system Chrome when ``PJUD_CHROME_PATH`` is set — same fingerprint
        humans use, less likely to trigger F5 Shape.
        """
        if self._browser is not None:
            logger.debug("Browser already started")
            return
        
        chrome_path = settings.PJUD_CHROME_PATH or None
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        if chrome_path:
            launch_args += [
                "--disable-sync",
                "--no-first-run",
                "--hide-scrollbars",
                "--mute-audio",
            ]
            logger.info("BrowserFactory using system Chrome: %s", chrome_path)
        
        start_time = time.perf_counter()
        logger.info("Starting fresh browser instance")
        
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            executable_path=chrome_path,
            args=launch_args,
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
        
        # Build storage_state so both cookies AND localStorage are present
        # when indexN.php first loads (PJUD checks localStorage at load time).
        storage_state = build_storage_state(session)
        if storage_state and storage_state.get("cookies"):
            logger.debug(
                f"Restoring {len(storage_state['cookies'])} cookies via storage_state"
            )

        # Create new browser context with stealth-friendly options:
        #   - Chrome 149 UA (matches system Chrome)
        #   - Full-HD viewport (configurable)
        #   - Chilean locale + timezone for PJUD
        #   - navigator.webdriver overridden via init script
        context_options = {
            "viewport": {
                "width": settings.PJUD_VIEWPORT_WIDTH,
                "height": settings.PJUD_VIEWPORT_HEIGHT,
            },
            "user_agent": _CHROME_UA,
            "locale": "es-CL",
            "timezone_id": "America/Santiago",
            "storage_state": storage_state,
        }

        self._context = await self._browser.new_context(**context_options)

        # Patch navigator.webdriver — the #1 fingerprint Shape checks
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        # Create new page
        self._page = await self._context.new_page()

        # Navigate to PJUD index page if session provided.
        # localStorage is already present from storage_state, so no post-
        # navigation Object.assign is needed.
        if session is not None:
            try:
                from app.scrapper.pjud.base import PJUD_INDEX_URL

                await self._page.goto(PJUD_INDEX_URL, timeout=60000, wait_until="domcontentloaded")

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
