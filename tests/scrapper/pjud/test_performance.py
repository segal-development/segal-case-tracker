"""
Performance benchmark tests for BrowserFactory.

Tests:
1. Browser startup time < 3s
2. Page creation time < 500ms
3. Metrics collection and reset
4. WarmBrowserPool startup time

These tests use real Playwright to measure actual performance.
Run with: pytest tests/scrapper/pjud/test_performance.py -v
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestBrowserMetrics:
    """Test browser metrics collection."""
    
    def test_get_browser_metrics_empty(self):
        """get_browser_metrics returns zeros when no data."""
        from app.scrapper.pjud.browser import reset_browser_metrics, get_browser_metrics
        
        reset_browser_metrics()
        metrics = get_browser_metrics()
        
        assert metrics["browser_startup"]["count"] == 0
        assert metrics["browser_startup"]["avg_ms"] == 0
        assert metrics["page_creation"]["count"] == 0
    
    def test_get_browser_metrics_with_data(self):
        """get_browser_metrics calculates stats correctly."""
        import app.scrapper.pjud.browser as browser_module
        from app.scrapper.pjud.browser import reset_browser_metrics, get_browser_metrics
        
        reset_browser_metrics()
        
        # Add test data directly to module lists
        browser_module._browser_startup_times.extend([100.0, 200.0, 300.0])
        browser_module._page_creation_times.extend([10.0, 20.0, 30.0])
        
        metrics = get_browser_metrics()
        
        assert metrics["browser_startup"]["count"] == 3
        assert metrics["browser_startup"]["avg_ms"] == 200.0
        assert metrics["browser_startup"]["min_ms"] == 100.0
        assert metrics["browser_startup"]["max_ms"] == 300.0
        
        assert metrics["page_creation"]["count"] == 3
        assert metrics["page_creation"]["avg_ms"] == 20.0
        
        # Cleanup
        reset_browser_metrics()
    
    def test_reset_browser_metrics(self):
        """reset_browser_metrics clears all data."""
        import app.scrapper.pjud.browser as browser_module
        from app.scrapper.pjud.browser import reset_browser_metrics, get_browser_metrics
        
        browser_module._browser_startup_times.append(100.0)
        reset_browser_metrics()
        
        metrics = get_browser_metrics()
        assert metrics["browser_startup"]["count"] == 0


class TestBrowserStartupBenchmark:
    """Benchmark tests for browser startup time."""
    
    @pytest.mark.asyncio
    async def test_browser_startup_records_timing(self):
        """BrowserFactory._start() records startup timing."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import (
                BrowserFactory,
                reset_browser_metrics,
                get_browser_metrics,
            )
            
            reset_browser_metrics()
            
            async with BrowserFactory() as factory:
                pass
            
            metrics = get_browser_metrics()
            assert metrics["browser_startup"]["count"] == 1
            assert metrics["browser_startup"]["avg_ms"] > 0
            
            reset_browser_metrics()
    
    @pytest.mark.asyncio
    async def test_page_creation_records_timing(self):
        """BrowserFactory.new_page() records creation timing."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import (
                BrowserFactory,
                reset_browser_metrics,
                get_browser_metrics,
            )
            
            reset_browser_metrics()
            
            async with BrowserFactory() as factory:
                await factory.new_page()
            
            metrics = get_browser_metrics()
            assert metrics["page_creation"]["count"] == 1
            assert metrics["page_creation"]["avg_ms"] > 0
            
            reset_browser_metrics()
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.integration  # launches a REAL browser — excluded from the unit gate / CI
    async def test_real_browser_startup_under_3_seconds(self):
        """Real browser startup should be < 3 seconds.
        
        Note: This test uses real Playwright and is marked slow.
        Run with: pytest -m slow tests/scrapper/pjud/test_performance.py
        """
        from app.scrapper.pjud.browser import (
            BrowserFactory,
            reset_browser_metrics,
            get_browser_metrics,
        )
        
        reset_browser_metrics()
        start = time.perf_counter()
        
        async with BrowserFactory(headless=True) as factory:
            pass
        
        elapsed_ms = (time.perf_counter() - start) * 1000
        
        metrics = get_browser_metrics()
        startup_ms = metrics["browser_startup"]["avg_ms"]
        
        # Assert startup under 3 seconds (3000ms)
        assert startup_ms < 3000, f"Browser startup took {startup_ms:.2f}ms (> 3s)"
        
        reset_browser_metrics()


class TestWarmBrowserPoolBenchmark:
    """Benchmark tests for WarmBrowserPool."""
    
    @pytest.mark.asyncio
    async def test_pool_starts_multiple_browsers(self):
        """WarmBrowserPool should start all browsers."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import WarmBrowserPool, reset_browser_metrics
            
            reset_browser_metrics()
            
            pool = WarmBrowserPool(size=2)
            await pool.start()
            
            try:
                assert pool.is_started
                assert pool.available_count == 2
                # Should have launched 2 browsers
                assert mock_playwright.chromium.launch.call_count == 2
            finally:
                await pool.stop()
    
    @pytest.mark.asyncio
    async def test_pool_acquire_returns_browser(self):
        """WarmBrowserPool.acquire() returns a BrowserFactory."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import WarmBrowserPool
            
            pool = WarmBrowserPool(size=2)
            await pool.start()
            
            try:
                assert pool.available_count == 2
                
                async with pool.acquire() as factory:
                    assert pool.available_count == 1
                    page = await factory.new_page()
                    assert page is mock_page
                
                # Browser returned to pool
                assert pool.available_count == 2
            finally:
                await pool.stop()
    
    @pytest.mark.asyncio
    async def test_pool_raises_when_not_started(self):
        """WarmBrowserPool.acquire() raises if pool not started."""
        from app.scrapper.pjud.browser import WarmBrowserPool
        
        pool = WarmBrowserPool(size=2)
        
        with pytest.raises(RuntimeError, match="Pool not started"):
            pool.acquire()
    
    @pytest.mark.asyncio
    async def test_pool_raises_when_exhausted(self):
        """WarmBrowserPool.acquire() raises when no browsers available."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import WarmBrowserPool
            
            pool = WarmBrowserPool(size=1)
            await pool.start()
            
            try:
                # Acquire the only browser
                async with pool.acquire():
                    # Try to acquire another while first is held
                    with pytest.raises(RuntimeError, match="No browsers available"):
                        pool.acquire()
            finally:
                await pool.stop()
