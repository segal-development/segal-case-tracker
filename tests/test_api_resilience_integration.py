"""
Tests for Resilience Integration in PJUD API Endpoints.

Tests verify that circuit breaker, metrics, and logging are properly
integrated into the API layer.
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.scrapper.pjud.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from app.scrapper.pjud.resilience.integration import (
    get_competency_circuit_breaker,
    record_scrape_success,
    record_scrape_error,
    get_circuit_state,
    reset_circuit,
    reset_circuit_sync,
)
from app.scrapper.pjud.observability.metrics import get_metrics


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset circuit breakers and metrics before each test."""
    # Reset circuit breakers (sync - direct state manipulation)
    for comp in ["civil", "laboral", "penal"]:
        cb = get_competency_circuit_breaker(comp)
        cb._state = CircuitState.CLOSED
        cb._failure_count = 0
        cb._success_count = 0
        cb._half_open_calls = 0
    
    # Reset metrics
    metrics = get_metrics()
    metrics.reset()
    
    yield
    
    # Cleanup after test
    for comp in ["civil", "laboral", "penal"]:
        cb = get_competency_circuit_breaker(comp)
        cb._state = CircuitState.CLOSED
        cb._failure_count = 0
        cb._success_count = 0
        cb._half_open_calls = 0


# ============================================================================
# CIRCUIT BREAKER INTEGRATION TESTS
# ============================================================================

class TestCircuitBreakerPerCompetency:
    """Test that each competency has its own circuit breaker."""
    
    def test_civil_has_own_circuit_breaker(self):
        """Civil competency has its own circuit breaker."""
        cb = get_competency_circuit_breaker("civil")
        assert cb.name == "pjud-civil"
    
    def test_laboral_has_own_circuit_breaker(self):
        """Laboral competency has its own circuit breaker."""
        cb = get_competency_circuit_breaker("laboral")
        assert cb.name == "pjud-laboral"
    
    def test_penal_has_own_circuit_breaker(self):
        """Penal competency has its own circuit breaker."""
        cb = get_competency_circuit_breaker("penal")
        assert cb.name == "pjud-penal"
    
    @pytest.mark.asyncio
    async def test_circuit_breakers_are_independent(self):
        """Failures in one competency don't affect others."""
        cb_civil = get_competency_circuit_breaker("civil")
        cb_laboral = get_competency_circuit_breaker("laboral")
        
        # Open civil circuit
        for _ in range(5):
            await cb_civil.record_failure()
        
        assert cb_civil.state == CircuitState.OPEN
        assert cb_laboral.state == CircuitState.CLOSED


class TestCircuitBreakerAPIIntegration:
    """Test circuit breaker integration with API endpoints."""
    
    def test_health_endpoint_shows_circuit_state(self, client):
        """Health endpoint shows circuit breaker state."""
        response = client.get("/api/v1/pjud/health")
        
        assert response.status_code == 200
        data = response.json()
        
        assert "circuit_breaker" in data
        assert data["circuit_breaker"]["state"] == "closed"
    
    @pytest.mark.asyncio
    async def test_returns_503_when_circuit_open(self, client):
        """API returns 503 when circuit is open."""
        # Open the circuit
        cb = get_competency_circuit_breaker("civil")
        for _ in range(5):
            await cb.record_failure()
        
        assert cb.state == CircuitState.OPEN
        
        # Try to get cases - should fail with 503
        response = client.get(
            "/api/v1/pjud/cases",
            params={"session_id": "fake-session"}
        )
        
        # Could be 401 (no session) or 503 (circuit open)
        # Circuit check happens first, so should be 503
        assert response.status_code == 503
        assert "circuit open" in response.json()["detail"].lower()
    
    @pytest.mark.asyncio
    async def test_circuit_check_before_session_check(self, client):
        """Circuit breaker is checked before session validation."""
        cb = get_competency_circuit_breaker("civil")
        for _ in range(5):
            await cb.record_failure()
        
        # Even with invalid session, circuit is checked first
        response = client.get(
            "/api/v1/pjud/cases",
            params={"session_id": "nonexistent"}
        )
        
        assert response.status_code == 503


# ============================================================================
# METRICS INTEGRATION TESTS
# ============================================================================

class TestMetricsIntegration:
    """Test metrics integration with API endpoints."""
    
    def test_metrics_endpoint_exists(self, client):
        """Metrics endpoint is accessible."""
        response = client.get("/api/v1/pjud/metrics")
        
        assert response.status_code == 200
        assert "pjud_cases_scraped_total" in response.text
    
    def test_metrics_format_is_prometheus(self, client):
        """Metrics are in Prometheus format."""
        response = client.get("/api/v1/pjud/metrics")
        
        text = response.text
        
        # Check for Prometheus format markers
        assert "# HELP" in text
        assert "# TYPE" in text
    
    def test_record_scrape_success_updates_metrics(self):
        """record_scrape_success updates metrics correctly."""
        metrics = get_metrics()
        initial = metrics.cases_scraped.get(competency="civil")
        
        record_scrape_success("civil", 10)
        
        assert metrics.cases_scraped.get(competency="civil") == initial + 10
    
    def test_record_scrape_error_updates_metrics(self):
        """record_scrape_error updates error metrics."""
        metrics = get_metrics()
        
        record_scrape_error("civil", "TimeoutError")
        
        assert metrics.errors_total.get(competency="civil", error_type="TimeoutError") == 1
    
    @pytest.mark.asyncio
    async def test_circuit_rejected_recorded_as_error(self, client):
        """Circuit rejection is recorded as error metric."""
        metrics = get_metrics()
        cb = get_competency_circuit_breaker("civil")
        
        # Open circuit
        for _ in range(5):
            await cb.record_failure()
        
        # Make request that gets rejected
        client.get("/api/v1/pjud/cases", params={"session_id": "fake"})
        
        # Check error was recorded
        assert metrics.errors_total.get(competency="civil", error_type="circuit_rejected") >= 1


class TestMetricsPerCompetency:
    """Test that metrics are tracked per competency."""
    
    def test_cases_scraped_per_competency(self):
        """Cases scraped tracked separately per competency."""
        record_scrape_success("civil", 10)
        record_scrape_success("laboral", 5)
        record_scrape_success("penal", 3)
        
        metrics = get_metrics()
        
        assert metrics.cases_scraped.get(competency="civil") == 10
        assert metrics.cases_scraped.get(competency="laboral") == 5
        assert metrics.cases_scraped.get(competency="penal") == 3
    
    def test_errors_per_competency(self):
        """Errors tracked separately per competency."""
        record_scrape_error("civil", "TimeoutError")
        record_scrape_error("laboral", "ConnectionError")
        
        metrics = get_metrics()
        
        assert metrics.errors_total.get(competency="civil", error_type="TimeoutError") == 1
        assert metrics.errors_total.get(competency="laboral", error_type="ConnectionError") == 1
        assert metrics.errors_total.get(competency="civil", error_type="ConnectionError") == 0


# ============================================================================
# HELPER FUNCTION TESTS
# ============================================================================

class TestIntegrationHelpers:
    """Test helper functions from integration module."""
    
    @pytest.mark.asyncio
    async def test_get_circuit_state(self):
        """get_circuit_state returns correct state string."""
        assert get_circuit_state("civil") == "closed"
        
        cb = get_competency_circuit_breaker("civil")
        for _ in range(5):
            await cb.record_failure()
        
        assert get_circuit_state("civil") == "open"
    
    @pytest.mark.asyncio
    async def test_reset_circuit(self):
        """reset_circuit closes the circuit."""
        cb = get_competency_circuit_breaker("civil")
        
        # Open it
        for _ in range(5):
            await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        
        # Reset it
        await reset_circuit("civil")
        assert cb.state == CircuitState.CLOSED
    
    def test_circuit_breaker_singleton(self):
        """Same circuit breaker returned for same competency."""
        cb1 = get_competency_circuit_breaker("civil")
        cb2 = get_competency_circuit_breaker("civil")
        
        assert cb1 is cb2


# ============================================================================
# API ENDPOINT RESILIENCE TESTS
# ============================================================================

class TestCasesEndpointResilience:
    """Test /cases endpoint resilience features."""
    
    def test_requires_session_id(self, client):
        """Endpoint requires session_id parameter."""
        # Reset circuit to ensure it's not the blocker
        reset_circuit_sync("civil")
        
        response = client.get("/api/v1/pjud/cases")
        
        assert response.status_code == 422  # Missing required param
    
    def test_rejects_invalid_session(self, client):
        """Endpoint rejects invalid session."""
        reset_circuit_sync("civil")
        
        response = client.get(
            "/api/v1/pjud/cases",
            params={"session_id": "invalid-session-id"}
        )
        
        assert response.status_code == 401


class TestLaboralEndpointResilience:
    """Test /laboral/cases endpoint resilience features."""
    
    @pytest.mark.asyncio
    async def test_has_circuit_breaker(self, client):
        """Laboral endpoint has circuit breaker protection."""
        cb = get_competency_circuit_breaker("laboral")
        for _ in range(5):
            await cb.record_failure()
        
        response = client.get(
            "/api/v1/pjud/laboral/cases",
            params={"session_id": "fake"}
        )
        
        assert response.status_code == 503


class TestPenalEndpointResilience:
    """Test /penal/cases endpoint resilience features."""
    
    @pytest.mark.asyncio
    async def test_has_circuit_breaker(self, client):
        """Penal endpoint has circuit breaker protection."""
        cb = get_competency_circuit_breaker("penal")
        for _ in range(5):
            await cb.record_failure()
        
        response = client.get(
            "/api/v1/pjud/penal/cases",
            params={"session_id": "fake"}
        )
        
        assert response.status_code == 503


# ============================================================================
# FULL FLOW INTEGRATION TESTS (MOCKED)
# ============================================================================

class TestFullFlowWithMocks:
    """Test full scraping flow with mocked scraper."""
    
    @pytest.mark.asyncio
    async def test_success_records_metrics(self):
        """Successful scrape records all metrics."""
        metrics = get_metrics()
        metrics.reset()
        
        # Simulate what happens in the endpoint
        start_time = time.time()
        
        # Simulate successful scrape
        cases_count = 15
        duration = 2.5
        
        cb = get_competency_circuit_breaker("civil")
        await cb.record_success()
        record_scrape_success("civil", cases_count)
        metrics.request_duration.observe(duration, competency="civil", endpoint="get_cases")
        
        # Verify metrics
        assert metrics.cases_scraped.get(competency="civil") == 15
        assert cb.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_failure_records_error_metrics(self):
        """Failed scrape records error metrics."""
        metrics = get_metrics()
        metrics.reset()
        
        # Simulate what happens on error
        cb = get_competency_circuit_breaker("civil")
        await cb.record_failure()
        record_scrape_error("civil", "TimeoutError")
        
        # Verify metrics
        assert metrics.errors_total.get(competency="civil", error_type="TimeoutError") == 1
    
    @pytest.mark.asyncio
    async def test_repeated_failures_open_circuit(self):
        """Repeated failures open the circuit breaker."""
        cb = get_competency_circuit_breaker("civil")
        cb._state = CircuitState.CLOSED
        cb._failure_count = 0
        
        # Simulate 5 failures
        for _ in range(5):
            await cb.record_failure()
            record_scrape_error("civil", "TimeoutError")
        
        assert cb.state == CircuitState.OPEN
