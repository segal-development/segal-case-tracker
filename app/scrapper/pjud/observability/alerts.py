"""
Webhook Alerts for PJUD Scraper.

Provides:
- AlertManager for sending alerts via HTTP webhook
- Support for different severity levels (info, warning, critical)
- Alert types: circuit_open, health_check_fail, structure_change, rate_limit_exceeded
- JSON payload with timestamp, severity, title, message, and context
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(Enum):
    """Pre-defined alert types."""
    CIRCUIT_OPEN = "circuit_open"
    HEALTH_CHECK_FAIL = "health_check_fail"
    STRUCTURE_CHANGE = "structure_change"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    LOGIN_FAILED = "login_failed"
    SCRAPING_ERROR = "scraping_error"


@dataclass
class Alert:
    """Alert payload.
    
    Attributes:
        severity: Alert severity (info, warning, critical)
        title: Short alert title
        message: Detailed alert message
        alert_type: Type of alert
        context: Additional context data
        timestamp: When the alert was created (auto-set)
    """
    severity: AlertSeverity
    title: str
    message: str
    alert_type: Optional[AlertType] = None
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "alert_type": self.alert_type.value if self.alert_type else None,
            "context": self.context,
        }


@dataclass
class AlertConfig:
    """Configuration for alert delivery.
    
    Attributes:
        webhook_url: URL to POST alerts to
        timeout: Request timeout in seconds
        retry_count: Number of retries on failure
        retry_delay: Delay between retries in seconds
        enabled: Whether alerting is enabled
    """
    webhook_url: Optional[str] = None
    timeout: float = 10.0
    retry_count: int = 3
    retry_delay: float = 1.0
    enabled: bool = True
    
    @classmethod
    def from_env(cls) -> "AlertConfig":
        """Create config from environment variables."""
        return cls(
            webhook_url=os.environ.get("PJUD_ALERT_WEBHOOK_URL"),
            timeout=float(os.environ.get("PJUD_ALERT_TIMEOUT", "10")),
            retry_count=int(os.environ.get("PJUD_ALERT_RETRY_COUNT", "3")),
            enabled=os.environ.get("PJUD_ALERTS_ENABLED", "true").lower() in ("true", "1", "yes"),
        )


class AlertManager:
    """Manages alert delivery via webhooks.
    
    Example:
        manager = get_alert_manager()
        
        await manager.send_alert(
            severity=AlertSeverity.CRITICAL,
            title="Circuit Breaker Open",
            message="PJUD Civil circuit breaker opened after 5 failures",
            alert_type=AlertType.CIRCUIT_OPEN,
            context={
                "competency": "civil",
                "failure_count": 5,
                "last_error": "Connection timeout",
            },
        )
    """
    
    def __init__(self, config: Optional[AlertConfig] = None):
        self.config = config or AlertConfig.from_env()
        self._callbacks: List[Callable[[Alert], None]] = []
        self._history: List[Alert] = []
        self._history_limit = 100
    
    @property
    def is_enabled(self) -> bool:
        """Check if alerting is enabled and configured."""
        return self.config.enabled and bool(self.config.webhook_url)
    
    def on_alert(self, callback: Callable[[Alert], None]) -> None:
        """Register a callback for alert events.
        
        Args:
            callback: Function to call when an alert is sent
        """
        self._callbacks.append(callback)
    
    async def send_alert(
        self,
        severity: AlertSeverity,
        title: str,
        message: str,
        alert_type: Optional[AlertType] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send an alert via webhook.
        
        Args:
            severity: Alert severity level
            title: Short alert title
            message: Detailed message
            alert_type: Optional alert type classification
            context: Additional context data
        
        Returns:
            True if alert was sent successfully, False otherwise
        """
        alert = Alert(
            severity=severity,
            title=title,
            message=message,
            alert_type=alert_type,
            context=context or {},
        )
        
        # Store in history
        self._history.append(alert)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]
        
        # Trigger callbacks
        for callback in self._callbacks:
            try:
                callback(alert)
            except Exception as e:
                logger.warning(f"Alert callback error: {e}")
        
        # Send webhook if enabled
        if not self.is_enabled:
            logger.debug(f"Alert not sent (disabled): {title}")
            return False
        
        return await self._send_webhook(alert)
    
    async def _send_webhook(self, alert: Alert) -> bool:
        """Send alert to webhook with retries."""
        payload = alert.to_dict()
        
        for attempt in range(self.config.retry_count + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        self.config.webhook_url,
                        json=payload,
                        timeout=self.config.timeout,
                        headers={"Content-Type": "application/json"},
                    )
                    
                    if response.status_code in (200, 201, 202, 204):
                        logger.info(f"Alert sent: {alert.title}")
                        return True
                    
                    logger.warning(
                        f"Webhook returned {response.status_code}: {response.text}"
                    )
                    
            except asyncio.TimeoutError:
                logger.warning(f"Webhook timeout (attempt {attempt + 1})")
            except httpx.ConnectError as e:
                logger.warning(f"Webhook connection error: {e}")
            except Exception as e:
                logger.error(f"Webhook error: {e}")
            
            # Retry delay
            if attempt < self.config.retry_count:
                await asyncio.sleep(self.config.retry_delay)
        
        logger.error(f"Failed to send alert after {self.config.retry_count + 1} attempts")
        return False
    
    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent alert history.
        
        Args:
            limit: Maximum number of alerts to return
        
        Returns:
            List of alert dictionaries, most recent first
        """
        return [a.to_dict() for a in reversed(self._history[-limit:])]
    
    def clear_history(self) -> None:
        """Clear alert history."""
        self._history.clear()


# Convenience functions for common alert types
async def alert_circuit_open(
    manager: AlertManager,
    competency: str,
    failure_count: int,
    last_error: str,
) -> bool:
    """Send circuit breaker open alert."""
    return await manager.send_alert(
        severity=AlertSeverity.CRITICAL,
        title="Circuit Breaker Open",
        message=f"PJUD {competency.title()} circuit breaker opened after {failure_count} failures",
        alert_type=AlertType.CIRCUIT_OPEN,
        context={
            "competency": competency,
            "failure_count": failure_count,
            "last_error": last_error,
        },
    )


async def alert_health_check_fail(
    manager: AlertManager,
    error: str,
    response_time_ms: Optional[int] = None,
) -> bool:
    """Send health check failure alert."""
    context = {"error": error}
    if response_time_ms is not None:
        context["response_time_ms"] = response_time_ms
    
    return await manager.send_alert(
        severity=AlertSeverity.WARNING,
        title="Health Check Failed",
        message=f"PJUD health check failed: {error}",
        alert_type=AlertType.HEALTH_CHECK_FAIL,
        context=context,
    )


async def alert_structure_change(
    manager: AlertManager,
    current_hash: str,
    baseline_hash: str,
) -> bool:
    """Send structure change detection alert."""
    return await manager.send_alert(
        severity=AlertSeverity.WARNING,
        title="Structure Change Detected",
        message="PJUD HTML structure has changed from baseline. Selectors may need updating.",
        alert_type=AlertType.STRUCTURE_CHANGE,
        context={
            "current_hash": current_hash,
            "baseline_hash": baseline_hash,
        },
    )


async def alert_rate_limit_exceeded(
    manager: AlertManager,
    competency: str,
    wait_time: float,
) -> bool:
    """Send rate limit exceeded alert."""
    return await manager.send_alert(
        severity=AlertSeverity.INFO,
        title="Rate Limit Exceeded",
        message=f"PJUD {competency.title()} rate limit exceeded, waiting {wait_time:.1f}s",
        alert_type=AlertType.RATE_LIMIT_EXCEEDED,
        context={
            "competency": competency,
            "wait_time_seconds": wait_time,
        },
    )


# Global alert manager instance
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Get the global alert manager instance."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


async def send_alert(
    severity: AlertSeverity,
    title: str,
    message: str,
    alert_type: Optional[AlertType] = None,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """Convenience function to send an alert using global manager.
    
    Example:
        await send_alert(
            AlertSeverity.CRITICAL,
            "Circuit Breaker Open",
            "PJUD Civil circuit is open",
            AlertType.CIRCUIT_OPEN,
            {"competency": "civil"},
        )
    """
    manager = get_alert_manager()
    return await manager.send_alert(
        severity=severity,
        title=title,
        message=message,
        alert_type=alert_type,
        context=context,
    )
