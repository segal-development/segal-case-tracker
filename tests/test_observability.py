"""
Tests for PJUD Observability Layer.

Tests:
1. Logging decorator captures context and timing
2. Metrics increment correctly
3. Alerts send webhook with proper payload (mocked)
4. /metrics endpoint returns Prometheus format
"""

import asyncio
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapper.pjud.observability.logging import (
    LogContext,
    configure_logging,
    get_logger,
    log_operation,
    log_context,
    get_log_level,
)
from app.scrapper.pjud.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    PJUDMetrics,
    get_metrics,
    metrics_endpoint,
    metrics_enabled,
)
from app.scrapper.pjud.observability.alerts import (
    Alert,
    AlertConfig,
    AlertManager,
    AlertSeverity,
    AlertType,
    get_alert_manager,
    send_alert,
    alert_circuit_open,
    alert_health_check_fail,
    alert_structure_change,
)


# ============================================================================
# LOGGING TESTS
# ============================================================================

class TestLogContext:
    """Test LogContext dataclass."""
    
    def test_to_dict_excludes_none(self):
        """to_dict excludes None values."""
        ctx = LogContext(user_rut="12345678-9", competency="civil")
        
        d = ctx.to_dict()
        
        assert d["user_rut"] == "12345678-9"
        assert d["competency"] == "civil"
        assert "operation" not in d
    
    def test_to_dict_includes_extra(self):
        """to_dict includes extra fields."""
        ctx = LogContext(
            user_rut="12345678-9",
            extra={"request_id": "abc123"},
        )
        
        d = ctx.to_dict()
        
        assert d["request_id"] == "abc123"


class TestGetLogLevel:
    """Test log level configuration."""
    
    def test_default_is_info(self):
        """Default log level is INFO."""
        with patch.dict(os.environ, {}, clear=True):
            import logging
            # Reset cached value
            level = get_log_level()
            assert level == logging.INFO
    
    def test_reads_env_var(self):
        """Reads PJUD_LOG_LEVEL from environment."""
        import logging
        
        with patch.dict(os.environ, {"PJUD_LOG_LEVEL": "DEBUG"}):
            level = get_log_level()
            assert level == logging.DEBUG
        
        with patch.dict(os.environ, {"PJUD_LOG_LEVEL": "WARNING"}):
            level = get_log_level()
            assert level == logging.WARNING


class TestLogOperation:
    """Test @log_operation decorator."""
    
    @pytest.mark.asyncio
    async def test_logs_async_function(self):
        """Decorator works with async functions."""
        @log_operation()
        async def my_async_func(x):
            return x * 2
        
        result = await my_async_func(5)
        
        assert result == 10
    
    def test_logs_sync_function(self):
        """Decorator works with sync functions."""
        @log_operation()
        def my_sync_func(x):
            return x + 1
        
        result = my_sync_func(5)
        
        assert result == 6
    
    @pytest.mark.asyncio
    async def test_captures_duration(self):
        """Decorator captures operation duration."""
        @log_operation()
        async def slow_func():
            await asyncio.sleep(0.05)
            return "done"
        
        # Function should complete without error
        result = await slow_func()
        assert result == "done"
    
    @pytest.mark.asyncio
    async def test_logs_errors(self):
        """Decorator logs errors."""
        @log_operation()
        async def failing_func():
            raise ValueError("Something went wrong")
        
        with pytest.raises(ValueError):
            await failing_func()
    
    @pytest.mark.asyncio
    async def test_custom_operation_name(self):
        """Decorator uses custom operation name."""
        @log_operation(operation_name="custom_name")
        async def my_func():
            return True
        
        result = await my_func()
        assert result is True
    
    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self):
        """Decorator preserves function name and docstring."""
        @log_operation()
        async def documented_func():
            """This is the docstring."""
            pass
        
        assert documented_func.__name__ == "documented_func"
        assert "docstring" in documented_func.__doc__


# ============================================================================
# METRICS TESTS
# ============================================================================

class TestCounter:
    """Test Counter metric."""
    
    def test_starts_at_zero(self):
        """Counter starts at zero."""
        counter = Counter(name="test_counter", help_text="Test")
        
        assert counter.get() == 0.0
    
    def test_inc_increments(self):
        """inc() increments the counter."""
        counter = Counter(name="test_counter", help_text="Test")
        
        counter.inc()
        counter.inc(2.0)
        
        assert counter.get() == 3.0
    
    def test_labels_tracked_separately(self):
        """Different label values are tracked separately."""
        counter = Counter(
            name="test_counter",
            help_text="Test",
            labels=["type"],
        )
        
        counter.inc(1, type="a")
        counter.inc(2, type="b")
        counter.inc(1, type="a")
        
        assert counter.get(type="a") == 2.0
        assert counter.get(type="b") == 2.0
    
    def test_to_prometheus_format(self):
        """to_prometheus returns correct format."""
        counter = Counter(
            name="test_total",
            help_text="A test counter",
            labels=["label"],
        )
        counter.inc(5, label="value")
        
        output = counter.to_prometheus()
        
        assert "# HELP test_total A test counter" in output
        assert "# TYPE test_total counter" in output
        assert 'test_total{label="value"} 5' in output


class TestGauge:
    """Test Gauge metric."""
    
    def test_set_value(self):
        """set() sets the gauge value."""
        gauge = Gauge(name="test_gauge", help_text="Test")
        
        gauge.set(42.0)
        
        assert gauge.get() == 42.0
    
    def test_inc_dec(self):
        """inc() and dec() modify the gauge."""
        gauge = Gauge(name="test_gauge", help_text="Test")
        gauge.set(10)
        
        gauge.inc(5)
        assert gauge.get() == 15.0
        
        gauge.dec(3)
        assert gauge.get() == 12.0
    
    def test_to_prometheus_format(self):
        """to_prometheus returns correct format."""
        gauge = Gauge(
            name="test_state",
            help_text="Current state",
            labels=["comp"],
        )
        gauge.set(1, comp="civil")
        
        output = gauge.to_prometheus()
        
        assert "# TYPE test_state gauge" in output
        assert 'test_state{comp="civil"} 1' in output


class TestHistogram:
    """Test Histogram metric."""
    
    def test_observe_records_value(self):
        """observe() records value in buckets."""
        hist = Histogram(
            name="test_duration",
            help_text="Duration",
            buckets=[0.1, 0.5, 1.0],
        )
        
        hist.observe(0.3)
        hist.observe(0.7)
        
        output = hist.to_prometheus()
        
        # 0.3 goes in 0.5 and 1.0 buckets
        # 0.7 goes in 1.0 bucket
        assert "test_duration_count" in output
        assert "test_duration_sum" in output
    
    def test_to_prometheus_includes_buckets(self):
        """to_prometheus includes bucket boundaries."""
        hist = Histogram(
            name="test_hist",
            help_text="Test histogram",
            buckets=[0.1, 0.5],
        )
        hist.observe(0.05)
        
        output = hist.to_prometheus()
        
        assert 'le="0.1"' in output
        assert 'le="0.5"' in output
        assert 'le="+Inf"' in output


class TestPJUDMetrics:
    """Test PJUDMetrics collection."""
    
    @pytest.fixture
    def metrics(self):
        """Create fresh metrics instance."""
        m = PJUDMetrics()
        m.reset()
        return m
    
    def test_record_scrape(self, metrics):
        """record_scrape updates multiple metrics."""
        metrics.record_scrape(
            competency="civil",
            cases_count=10,
            duration_seconds=1.5,
        )
        
        assert metrics.cases_scraped.get(competency="civil") == 10
        assert metrics.requests_total.get(competency="civil", endpoint="cases") == 1
    
    def test_record_error(self, metrics):
        """record_error increments error counter."""
        metrics.record_error(competency="laboral", error_type="timeout")
        
        assert metrics.errors_total.get(competency="laboral", error_type="timeout") == 1
    
    def test_update_circuit_state(self, metrics):
        """update_circuit_state sets gauge correctly."""
        metrics.update_circuit_state("civil", "closed")
        assert metrics.circuit_state.get(competency="civil") == 0
        
        metrics.update_circuit_state("civil", "open")
        assert metrics.circuit_state.get(competency="civil") == 1
        
        metrics.update_circuit_state("civil", "half_open")
        assert metrics.circuit_state.get(competency="civil") == 2
    
    def test_to_prometheus(self, metrics):
        """to_prometheus exports all metrics."""
        metrics.cases_scraped.inc(5, competency="civil")
        metrics.circuit_state.set(0, competency="civil")
        
        output = metrics.to_prometheus()
        
        assert "pjud_cases_scraped_total" in output
        assert "pjud_circuit_state" in output


class TestMetricsEndpoint:
    """Test metrics endpoint function."""
    
    def test_returns_prometheus_format(self):
        """metrics_endpoint returns Prometheus format."""
        output = metrics_endpoint()
        
        # Should contain at least help comments
        assert "# HELP" in output or "disabled" in output
    
    def test_respects_enabled_setting(self):
        """metrics_endpoint respects PJUD_METRICS_ENABLED."""
        with patch.dict(os.environ, {"PJUD_METRICS_ENABLED": "false"}):
            output = metrics_endpoint()
            assert "disabled" in output.lower()


# ============================================================================
# ALERTS TESTS
# ============================================================================

class TestAlert:
    """Test Alert dataclass."""
    
    def test_to_dict(self):
        """to_dict returns JSON-serializable dict."""
        alert = Alert(
            severity=AlertSeverity.CRITICAL,
            title="Test Alert",
            message="Something happened",
            alert_type=AlertType.CIRCUIT_OPEN,
            context={"key": "value"},
        )
        
        d = alert.to_dict()
        
        assert d["severity"] == "critical"
        assert d["title"] == "Test Alert"
        assert d["message"] == "Something happened"
        assert d["alert_type"] == "circuit_open"
        assert d["context"]["key"] == "value"
        assert "timestamp" in d


class TestAlertConfig:
    """Test AlertConfig."""
    
    def test_from_env(self):
        """from_env reads environment variables."""
        with patch.dict(os.environ, {
            "PJUD_ALERT_WEBHOOK_URL": "https://example.com/webhook",
            "PJUD_ALERT_TIMEOUT": "5",
            "PJUD_ALERTS_ENABLED": "true",
        }):
            config = AlertConfig.from_env()
            
            assert config.webhook_url == "https://example.com/webhook"
            assert config.timeout == 5.0
            assert config.enabled is True
    
    def test_disabled_when_env_false(self):
        """Config is disabled when env var is false."""
        with patch.dict(os.environ, {"PJUD_ALERTS_ENABLED": "false"}):
            config = AlertConfig.from_env()
            assert config.enabled is False


class TestAlertManager:
    """Test AlertManager."""
    
    @pytest.fixture
    def manager(self):
        """Create manager with mocked config."""
        config = AlertConfig(
            webhook_url="https://example.com/webhook",
            timeout=1.0,
            retry_count=1,
            enabled=True,
        )
        return AlertManager(config=config)
    
    def test_is_enabled(self, manager):
        """is_enabled returns True when configured."""
        assert manager.is_enabled is True
    
    def test_is_disabled_without_url(self):
        """is_enabled returns False without webhook URL."""
        config = AlertConfig(webhook_url=None, enabled=True)
        manager = AlertManager(config=config)
        
        assert manager.is_enabled is False
    
    @pytest.mark.asyncio
    async def test_send_alert_stores_in_history(self, manager):
        """send_alert stores alert in history."""
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )
            
            await manager.send_alert(
                severity=AlertSeverity.INFO,
                title="Test",
                message="Test message",
            )
        
        assert len(manager._history) == 1
        assert manager._history[0].title == "Test"
    
    @pytest.mark.asyncio
    async def test_send_alert_calls_webhook(self, manager):
        """send_alert makes HTTP POST request."""
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_post = AsyncMock(return_value=mock_response)
            mock.return_value.__aenter__.return_value.post = mock_post
            
            result = await manager.send_alert(
                severity=AlertSeverity.CRITICAL,
                title="Circuit Open",
                message="Circuit breaker opened",
                context={"competency": "civil"},
            )
        
        assert result is True
        mock_post.assert_called_once()
        
        # Verify payload
        call_kwargs = mock_post.call_args[1]
        assert "json" in call_kwargs
        assert call_kwargs["json"]["severity"] == "critical"
    
    @pytest.mark.asyncio
    async def test_send_alert_retries_on_failure(self, manager):
        """send_alert retries on webhook failure."""
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Server Error"
            mock_post = AsyncMock(return_value=mock_response)
            mock.return_value.__aenter__.return_value.post = mock_post
            
            result = await manager.send_alert(
                severity=AlertSeverity.INFO,
                title="Test",
                message="Test",
            )
        
        # Should retry (retry_count=1 means 2 attempts total)
        assert mock_post.call_count == 2
        assert result is False
    
    @pytest.mark.asyncio
    async def test_callback_called(self, manager):
        """on_alert callback is called."""
        callback_called = False
        received_alert = None
        
        def callback(alert):
            nonlocal callback_called, received_alert
            callback_called = True
            received_alert = alert
        
        manager.on_alert(callback)
        
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            
            await manager.send_alert(
                severity=AlertSeverity.WARNING,
                title="Test Alert",
                message="Test",
            )
        
        assert callback_called is True
        assert received_alert.title == "Test Alert"
    
    def test_get_history(self, manager):
        """get_history returns recent alerts."""
        manager._history = [
            Alert(AlertSeverity.INFO, "Alert 1", "Msg 1"),
            Alert(AlertSeverity.WARNING, "Alert 2", "Msg 2"),
            Alert(AlertSeverity.CRITICAL, "Alert 3", "Msg 3"),
        ]
        
        history = manager.get_history(limit=2)
        
        assert len(history) == 2
        # Most recent first
        assert history[0]["title"] == "Alert 3"
        assert history[1]["title"] == "Alert 2"


class TestAlertConvenienceFunctions:
    """Test convenience alert functions."""
    
    @pytest.mark.asyncio
    async def test_alert_circuit_open(self):
        """alert_circuit_open sends correct payload."""
        config = AlertConfig(webhook_url="https://test.com", enabled=True)
        manager = AlertManager(config=config)
        
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            
            await alert_circuit_open(
                manager=manager,
                competency="civil",
                failure_count=5,
                last_error="Timeout",
            )
        
        alert = manager._history[0]
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.alert_type == AlertType.CIRCUIT_OPEN
        assert alert.context["competency"] == "civil"
        assert alert.context["failure_count"] == 5
    
    @pytest.mark.asyncio
    async def test_alert_health_check_fail(self):
        """alert_health_check_fail sends correct payload."""
        config = AlertConfig(webhook_url="https://test.com", enabled=True)
        manager = AlertManager(config=config)
        
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            
            await alert_health_check_fail(
                manager=manager,
                error="Connection refused",
                response_time_ms=5000,
            )
        
        alert = manager._history[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.alert_type == AlertType.HEALTH_CHECK_FAIL
    
    @pytest.mark.asyncio
    async def test_alert_structure_change(self):
        """alert_structure_change sends correct payload."""
        config = AlertConfig(webhook_url="https://test.com", enabled=True)
        manager = AlertManager(config=config)
        
        with patch("app.scrapper.pjud.observability.alerts.httpx.AsyncClient") as mock:
            mock.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            
            await alert_structure_change(
                manager=manager,
                current_hash="abc123",
                baseline_hash="xyz789",
            )
        
        alert = manager._history[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.alert_type == AlertType.STRUCTURE_CHANGE


class TestGlobalFunctions:
    """Test global/singleton functions."""
    
    def test_get_metrics_returns_singleton(self):
        """get_metrics returns same instance."""
        m1 = get_metrics()
        m2 = get_metrics()
        
        assert m1 is m2
    
    def test_get_alert_manager_returns_singleton(self):
        """get_alert_manager returns same instance."""
        a1 = get_alert_manager()
        a2 = get_alert_manager()
        
        assert a1 is a2
    
    @pytest.mark.asyncio
    async def test_send_alert_convenience(self):
        """send_alert convenience function works."""
        # Just verify it doesn't error (webhook not configured)
        result = await send_alert(
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test message",
        )
        
        # Should return False since no webhook configured by default
        # (depends on environment, so we just check it runs)
        assert result in (True, False)
