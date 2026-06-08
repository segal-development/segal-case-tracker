"""
Prometheus-style Metrics for PJUD Scraper.

Provides:
- Counters: pjud_cases_scraped_total, pjud_requests_total, pjud_errors_total
- Histograms: pjud_request_duration_seconds
- Gauges: pjud_circuit_state (0=closed, 1=open, 2=half_open)
- /metrics endpoint in Prometheus text format

Note: This is a lightweight implementation that doesn't require the
prometheus_client library. For production with Prometheus server,
consider using the official prometheus_client package.
"""

import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Counter:
    """Prometheus-style counter metric."""
    name: str
    help_text: str
    labels: List[str] = field(default_factory=list)
    _values: Dict[Tuple[str, ...], float] = field(default_factory=lambda: defaultdict(float))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    def inc(self, value: float = 1.0, **label_values) -> None:
        """Increment the counter."""
        key = self._make_key(label_values)
        with self._lock:
            self._values[key] += value
    
    def _make_key(self, label_values: Dict[str, str]) -> Tuple[str, ...]:
        """Create a key from label values."""
        return tuple(label_values.get(l, "") for l in self.labels)
    
    def get(self, **label_values) -> float:
        """Get current counter value."""
        key = self._make_key(label_values)
        return self._values.get(key, 0.0)
    
    def to_prometheus(self) -> str:
        """Format as Prometheus text."""
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        
        for key, value in self._values.items():
            if self.labels:
                label_str = ",".join(
                    f'{l}="{v}"' for l, v in zip(self.labels, key)
                )
                lines.append(f"{self.name}{{{label_str}}} {value}")
            else:
                lines.append(f"{self.name} {value}")
        
        return "\n".join(lines)


@dataclass
class Gauge:
    """Prometheus-style gauge metric."""
    name: str
    help_text: str
    labels: List[str] = field(default_factory=list)
    _values: Dict[Tuple[str, ...], float] = field(default_factory=lambda: defaultdict(float))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    def set(self, value: float, **label_values) -> None:
        """Set the gauge value."""
        key = self._make_key(label_values)
        with self._lock:
            self._values[key] = value
    
    def inc(self, value: float = 1.0, **label_values) -> None:
        """Increment the gauge."""
        key = self._make_key(label_values)
        with self._lock:
            self._values[key] += value
    
    def dec(self, value: float = 1.0, **label_values) -> None:
        """Decrement the gauge."""
        key = self._make_key(label_values)
        with self._lock:
            self._values[key] -= value
    
    def _make_key(self, label_values: Dict[str, str]) -> Tuple[str, ...]:
        """Create a key from label values."""
        return tuple(label_values.get(l, "") for l in self.labels)
    
    def get(self, **label_values) -> float:
        """Get current gauge value."""
        key = self._make_key(label_values)
        return self._values.get(key, 0.0)
    
    def to_prometheus(self) -> str:
        """Format as Prometheus text."""
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
        ]
        
        for key, value in self._values.items():
            if self.labels:
                label_str = ",".join(
                    f'{l}="{v}"' for l, v in zip(self.labels, key)
                )
                lines.append(f"{self.name}{{{label_str}}} {value}")
            else:
                lines.append(f"{self.name} {value}")
        
        return "\n".join(lines)


@dataclass
class Histogram:
    """Prometheus-style histogram metric.
    
    Tracks value distribution in pre-defined buckets.
    """
    name: str
    help_text: str
    labels: List[str] = field(default_factory=list)
    buckets: List[float] = field(default_factory=lambda: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
    
    # Internal: bucket_counts[label_key][bucket_upper] = count
    _bucket_counts: Dict[Tuple[str, ...], Dict[float, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    _sums: Dict[Tuple[str, ...], float] = field(default_factory=lambda: defaultdict(float))
    _counts: Dict[Tuple[str, ...], int] = field(default_factory=lambda: defaultdict(int))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    def observe(self, value: float, **label_values) -> None:
        """Record a value observation."""
        key = self._make_key(label_values)
        with self._lock:
            self._sums[key] += value
            self._counts[key] += 1
            
            for bucket in self.buckets:
                if value <= bucket:
                    self._bucket_counts[key][bucket] += 1
            # +Inf bucket always gets incremented
            self._bucket_counts[key][float("inf")] += 1
    
    def _make_key(self, label_values: Dict[str, str]) -> Tuple[str, ...]:
        """Create a key from label values."""
        return tuple(label_values.get(l, "") for l in self.labels)
    
    def to_prometheus(self) -> str:
        """Format as Prometheus text."""
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        
        for key in self._bucket_counts.keys():
            label_base = ""
            if self.labels:
                label_base = ",".join(
                    f'{l}="{v}"' for l, v in zip(self.labels, key)
                )
            
            # Bucket values (cumulative)
            cumulative = 0
            for bucket in self.buckets + [float("inf")]:
                cumulative += self._bucket_counts[key].get(bucket, 0) - cumulative
                le = "+Inf" if bucket == float("inf") else str(bucket)
                
                if label_base:
                    lines.append(f'{self.name}_bucket{{{label_base},le="{le}"}} {self._bucket_counts[key].get(bucket, 0)}')
                else:
                    lines.append(f'{self.name}_bucket{{le="{le}"}} {self._bucket_counts[key].get(bucket, 0)}')
            
            # Sum and count
            if label_base:
                lines.append(f"{self.name}_sum{{{label_base}}} {self._sums[key]}")
                lines.append(f"{self.name}_count{{{label_base}}} {self._counts[key]}")
            else:
                lines.append(f"{self.name}_sum {self._sums[key]}")
                lines.append(f"{self.name}_count {self._counts[key]}")
        
        return "\n".join(lines)


class PJUDMetrics:
    """Collection of PJUD-specific metrics.
    
    Provides counters, gauges, and histograms for monitoring
    scraper health and performance.
    
    Example:
        metrics = get_metrics()
        
        # Increment cases scraped
        metrics.cases_scraped.inc(competency="civil")
        
        # Record request duration
        metrics.request_duration.observe(0.5, competency="civil")
        
        # Update circuit state
        metrics.circuit_state.set(0, competency="civil")  # closed
        metrics.circuit_state.set(1, competency="civil")  # open
        metrics.circuit_state.set(2, competency="civil")  # half_open
    """
    
    def __init__(self):
        # Counters
        self.cases_scraped = Counter(
            name="pjud_cases_scraped_total",
            help_text="Total number of cases scraped from PJUD",
            labels=["competency"],
        )
        
        self.requests_total = Counter(
            name="pjud_requests_total",
            help_text="Total number of requests to PJUD",
            labels=["competency", "endpoint"],
        )
        
        self.errors_total = Counter(
            name="pjud_errors_total",
            help_text="Total number of errors during PJUD scraping",
            labels=["competency", "error_type"],
        )
        
        # Histograms
        self.request_duration = Histogram(
            name="pjud_request_duration_seconds",
            help_text="Duration of PJUD requests in seconds",
            labels=["competency", "endpoint"],
            buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
        )
        
        # Gauges
        self.circuit_state = Gauge(
            name="pjud_circuit_state",
            help_text="Circuit breaker state (0=closed, 1=open, 2=half_open)",
            labels=["competency"],
        )
        
        self.active_sessions = Gauge(
            name="pjud_active_sessions",
            help_text="Number of active PJUD sessions",
            labels=["competency"],
        )
        
        # Track all metrics for easy export
        self._metrics = [
            self.cases_scraped,
            self.requests_total,
            self.errors_total,
            self.request_duration,
            self.circuit_state,
            self.active_sessions,
        ]
    
    def record_scrape(
        self,
        competency: str,
        cases_count: int,
        duration_seconds: float,
    ) -> None:
        """Record a successful scrape operation.
        
        Args:
            competency: The competency type (civil, laboral, penal)
            cases_count: Number of cases scraped
            duration_seconds: Time taken in seconds
        """
        self.cases_scraped.inc(cases_count, competency=competency)
        self.requests_total.inc(competency=competency, endpoint="cases")
        self.request_duration.observe(duration_seconds, competency=competency, endpoint="cases")
    
    def record_error(
        self,
        competency: str,
        error_type: str,
    ) -> None:
        """Record an error.
        
        Args:
            competency: The competency type
            error_type: The type of error (e.g., "timeout", "circuit_open")
        """
        self.errors_total.inc(competency=competency, error_type=error_type)
    
    def update_circuit_state(
        self,
        competency: str,
        state: str,
    ) -> None:
        """Update circuit breaker state gauge.
        
        Args:
            competency: The competency type
            state: One of "closed", "open", "half_open"
        """
        state_map = {"closed": 0, "open": 1, "half_open": 2}
        value = state_map.get(state, 0)
        self.circuit_state.set(value, competency=competency)
    
    def to_prometheus(self) -> str:
        """Export all metrics in Prometheus text format."""
        parts = []
        for metric in self._metrics:
            output = metric.to_prometheus()
            if output.strip():
                parts.append(output)
        return "\n\n".join(parts)
    
    def reset(self) -> None:
        """Reset all metrics (useful for testing)."""
        for metric in self._metrics:
            if hasattr(metric, "_values"):
                metric._values.clear()
            if hasattr(metric, "_bucket_counts"):
                metric._bucket_counts.clear()
            if hasattr(metric, "_sums"):
                metric._sums.clear()
            if hasattr(metric, "_counts"):
                metric._counts.clear()


# Global metrics instance
_metrics: Optional[PJUDMetrics] = None


def get_metrics() -> PJUDMetrics:
    """Get the global metrics instance."""
    global _metrics
    if _metrics is None:
        _metrics = PJUDMetrics()
    return _metrics


def metrics_enabled() -> bool:
    """Check if metrics collection is enabled."""
    return os.environ.get("PJUD_METRICS_ENABLED", "true").lower() in ("true", "1", "yes")


def metrics_endpoint() -> str:
    """Get formatted metrics for the /metrics endpoint.
    
    Returns:
        Prometheus text format metrics string
    """
    if not metrics_enabled():
        return "# Metrics disabled\n"
    
    metrics = get_metrics()
    return metrics.to_prometheus()
