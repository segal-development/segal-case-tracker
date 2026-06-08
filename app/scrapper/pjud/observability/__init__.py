"""
PJUD Observability Package.

Production-grade observability for PJUD scraping:
- Structured JSON logging with context propagation
- Prometheus-style metrics (counters, histograms, gauges)
- Webhook alerts for critical events
"""

from app.scrapper.pjud.observability.logging import (
    configure_logging,
    get_logger,
    log_operation,
    LogContext,
)

from app.scrapper.pjud.observability.metrics import (
    PJUDMetrics,
    get_metrics,
    metrics_endpoint,
)

from app.scrapper.pjud.observability.alerts import (
    AlertManager,
    AlertSeverity,
    get_alert_manager,
    send_alert,
)


__all__ = [
    # Logging
    "configure_logging",
    "get_logger",
    "log_operation",
    "LogContext",
    
    # Metrics
    "PJUDMetrics",
    "get_metrics",
    "metrics_endpoint",
    
    # Alerts
    "AlertManager",
    "AlertSeverity",
    "get_alert_manager",
    "send_alert",
]
