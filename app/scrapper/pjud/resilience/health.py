"""
Health Checker for PJUD portal.

Monitors PJUD availability and detects HTML structure changes.
Runs as background task, triggers alerts on failures.
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)


PJUD_HOME_URL = "https://oficinajudicialvirtual.pjud.cl/home/index.php"

# Key elements to hash for structure detection
STRUCTURE_SELECTORS = [
    "form",               # Login form
    "input[name=rut]",    # RUT input
    "g-recaptcha",        # Captcha element
    ".nav",               # Navigation
]


class HealthStatus(Enum):
    """Health check status levels."""
    OK = "ok"               # Fully operational
    DEGRADED = "degraded"   # Partially working
    DOWN = "down"           # Unavailable
    UNKNOWN = "unknown"     # Not yet checked


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    status: HealthStatus
    response_time_ms: int
    structure_changed: bool
    current_hash: Optional[str]
    baseline_hash: Optional[str]
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "status": self.status.value,
            "response_time_ms": self.response_time_ms,
            "structure_changed": self.structure_changed,
            "current_hash": self.current_hash,
            "baseline_hash": self.baseline_hash,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class HealthChecker:
    """Background health checker for PJUD portal.
    
    Monitors availability and structure changes.
    
    Example:
        checker = HealthChecker()
        
        # Single check
        result = await checker.check()
        
        # Start background monitoring
        await checker.start(interval=300)  # Every 5 minutes
    """
    url: str = PJUD_HOME_URL
    timeout: float = 30.0
    
    # Internal state
    _baseline_hash: Optional[str] = field(default=None, init=False)
    _last_result: Optional[HealthCheckResult] = field(default=None, init=False)
    _running: bool = field(default=False, init=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False)
    _on_failure_callbacks: List[Callable] = field(default_factory=list, init=False)
    _on_structure_change_callbacks: List[Callable] = field(default_factory=list, init=False)
    
    @property
    def baseline_hash(self) -> Optional[str]:
        """Current baseline hash for structure comparison."""
        return self._baseline_hash
    
    @property
    def last_result(self) -> Optional[HealthCheckResult]:
        """Last health check result."""
        return self._last_result
    
    @property
    def is_running(self) -> bool:
        """Whether background monitoring is active."""
        return self._running
    
    def on_failure(self, callback: Callable) -> None:
        """Register callback for health check failures.
        
        Callback receives: (result: HealthCheckResult)
        """
        self._on_failure_callbacks.append(callback)
    
    def on_structure_change(self, callback: Callable) -> None:
        """Register callback for structure changes.
        
        Callback receives: (result: HealthCheckResult)
        """
        self._on_structure_change_callbacks.append(callback)
    
    def _compute_structure_hash(self, html: str) -> str:
        """Compute hash of key structural elements.
        
        Ignores dynamic content (timestamps, tokens, etc.)
        by focusing on structural patterns.
        """
        # Extract structural elements
        patterns = [
            r'<form[^>]*>',          # Form tags
            r'<input[^>]*name="[^"]*"[^>]*>',  # Input fields
            r'<div[^>]*class="[^"]*"[^>]*>',   # Div classes
            r'<nav[^>]*>',           # Navigation
            r'data-sitekey="[^"]*"', # reCAPTCHA sitekey
        ]
        
        structural_content = []
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            structural_content.extend(matches)
        
        # Sort for consistency
        structural_content.sort()
        
        # Compute hash
        content_str = "||".join(structural_content)
        return hashlib.sha256(content_str.encode()).hexdigest()[:16]
    
    async def check(self) -> HealthCheckResult:
        """Perform a health check on PJUD portal.
        
        Returns:
            HealthCheckResult with status and metrics
        """
        start_time = time.time()
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.url,
                    timeout=self.timeout,
                    follow_redirects=True,
                )
            
            response_time = int((time.time() - start_time) * 1000)
            
            # Check response status
            if response.status_code >= 500:
                result = HealthCheckResult(
                    status=HealthStatus.DOWN,
                    response_time_ms=response_time,
                    structure_changed=False,
                    current_hash=None,
                    baseline_hash=self._baseline_hash,
                    error=f"HTTP {response.status_code}",
                )
            elif response.status_code >= 400:
                result = HealthCheckResult(
                    status=HealthStatus.DEGRADED,
                    response_time_ms=response_time,
                    structure_changed=False,
                    current_hash=None,
                    baseline_hash=self._baseline_hash,
                    error=f"HTTP {response.status_code}",
                )
            else:
                # Check structure
                html = response.text
                current_hash = self._compute_structure_hash(html)
                
                # Set baseline on first successful check
                if self._baseline_hash is None:
                    self._baseline_hash = current_hash
                    logger.info(f"Health check baseline set: {current_hash}")
                
                structure_changed = current_hash != self._baseline_hash
                
                # Determine status
                if response_time > 10000:  # > 10 seconds
                    status = HealthStatus.DEGRADED
                else:
                    status = HealthStatus.OK
                
                result = HealthCheckResult(
                    status=status,
                    response_time_ms=response_time,
                    structure_changed=structure_changed,
                    current_hash=current_hash,
                    baseline_hash=self._baseline_hash,
                )
                
        except asyncio.TimeoutError:
            result = HealthCheckResult(
                status=HealthStatus.DOWN,
                response_time_ms=int(self.timeout * 1000),
                structure_changed=False,
                current_hash=None,
                baseline_hash=self._baseline_hash,
                error="Request timeout",
            )
        except httpx.ConnectError as e:
            result = HealthCheckResult(
                status=HealthStatus.DOWN,
                response_time_ms=int((time.time() - start_time) * 1000),
                structure_changed=False,
                current_hash=None,
                baseline_hash=self._baseline_hash,
                error=f"Connection error: {e}",
            )
        except Exception as e:
            result = HealthCheckResult(
                status=HealthStatus.DOWN,
                response_time_ms=int((time.time() - start_time) * 1000),
                structure_changed=False,
                current_hash=None,
                baseline_hash=self._baseline_hash,
                error=str(e),
            )
        
        self._last_result = result
        
        # Trigger callbacks
        if result.status in (HealthStatus.DOWN, HealthStatus.DEGRADED):
            for callback in self._on_failure_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(result)
                    else:
                        callback(result)
                except Exception as e:
                    logger.error(f"Health check callback error: {e}")
        
        if result.structure_changed:
            for callback in self._on_structure_change_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(result)
                    else:
                        callback(result)
                except Exception as e:
                    logger.error(f"Structure change callback error: {e}")
        
        logger.debug(
            f"Health check: status={result.status.value}, "
            f"time={result.response_time_ms}ms, "
            f"structure_changed={result.structure_changed}"
        )
        
        return result
    
    def detect_structure_change(self) -> bool:
        """Check if structure has changed since baseline.
        
        Returns:
            True if structure has changed
        """
        if self._last_result is None:
            return False
        return self._last_result.structure_changed
    
    def update_baseline(self, new_hash: Optional[str] = None) -> None:
        """Update the baseline hash.
        
        Call this after verifying a structure change is expected.
        
        Args:
            new_hash: New baseline hash (or use last check's hash)
        """
        if new_hash:
            self._baseline_hash = new_hash
        elif self._last_result and self._last_result.current_hash:
            self._baseline_hash = self._last_result.current_hash
        
        logger.info(f"Health check baseline updated: {self._baseline_hash}")
    
    async def start(self, interval: int = 300) -> None:
        """Start background health monitoring.
        
        Args:
            interval: Seconds between checks (default: 5 minutes)
        """
        if self._running:
            logger.warning("Health checker already running")
            return
        
        self._running = True
        
        async def monitor_loop():
            while self._running:
                try:
                    await self.check()
                except Exception as e:
                    logger.error(f"Health check error: {e}")
                
                await asyncio.sleep(interval)
        
        self._task = asyncio.create_task(monitor_loop())
        logger.info(f"Health checker started with {interval}s interval")
    
    async def stop(self) -> None:
        """Stop background health monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        logger.info("Health checker stopped")
    
    def get_status(self) -> dict:
        """Get current health checker status."""
        return {
            "running": self._running,
            "baseline_hash": self._baseline_hash,
            "last_check": self._last_result.to_dict() if self._last_result else None,
        }


# Global health checker instance
_health_checker: Optional[HealthChecker] = None


def get_health_checker() -> HealthChecker:
    """Get or create the global health checker.
    
    Returns:
        HealthChecker instance
    """
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker
