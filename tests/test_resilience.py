"""
Tests for PJUD Resilience Layer.

Tests:
1. Circuit breaker state transitions
2. Retry with exponential backoff timing
3. Rate limiter token bucket
4. Health check detection
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapper.pjud.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    circuit_breaker,
    get_circuit_breaker,
)
from app.scrapper.pjud.resilience.retry import (
    RetryConfig,
    with_retry,
    calculate_delay,
    retry_async,
)
from app.scrapper.pjud.resilience.rate_limiter import (
    TokenBucketLimiter,
    RateLimiterConfig,
    rate_limit,
    get_rate_limiter,
)
from app.scrapper.pjud.resilience.health import (
    HealthChecker,
    HealthStatus,
    HealthCheckResult,
    get_health_checker,
)
from app.scrapper.pjud.exceptions import CircuitOpenError, RateLimitError


# ============================================================================
# CIRCUIT BREAKER TESTS
# ============================================================================

class TestCircuitBreakerConfig:
    """Test CircuitBreakerConfig defaults."""
    
    def test_default_values(self):
        config = CircuitBreakerConfig()
        
        assert config.failure_threshold == 5
        assert config.recovery_timeout == 60
        assert config.half_open_max_calls == 3
        assert config.success_threshold == 2
    
    def test_custom_values(self):
        config = CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout=30,
            half_open_max_calls=2,
            success_threshold=1,
        )
        
        assert config.failure_threshold == 3
        assert config.recovery_timeout == 30


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""
    
    @pytest.fixture
    def cb(self):
        """Create a fresh circuit breaker."""
        return CircuitBreaker(
            name="test-cb",
            config=CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=1,  # Short for testing
                half_open_max_calls=2,
                success_threshold=2,
            ),
        )
    
    @pytest.mark.asyncio
    async def test_starts_closed(self, cb):
        """Circuit breaker starts in closed state."""
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed is True
        assert cb.is_open is False
    
    @pytest.mark.asyncio
    async def test_stays_closed_on_success(self, cb):
        """Circuit stays closed after successful calls."""
        async def success():
            return "ok"
        
        result = await cb.call(success)
        
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self, cb):
        """Circuit opens after failure threshold."""
        async def fail():
            raise TimeoutError("Connection failed")
        
        for i in range(3):
            with pytest.raises(TimeoutError):
                await cb.call(fail)
        
        assert cb.state == CircuitState.OPEN
        assert cb.is_open is True
    
    @pytest.mark.asyncio
    async def test_open_circuit_fails_fast(self, cb):
        """Open circuit rejects calls immediately."""
        # Force open
        for _ in range(3):
            await cb.record_failure()
        
        assert cb.is_open
        
        async def should_not_be_called():
            raise AssertionError("Should not be called")
        
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb.call(should_not_be_called)
        
        assert exc_info.value.competencia == "test-cb"
    
    @pytest.mark.asyncio
    async def test_transitions_to_half_open(self, cb):
        """Circuit transitions to half-open after recovery timeout."""
        # Force open
        for _ in range(3):
            await cb.record_failure()
        
        assert cb.state == CircuitState.OPEN
        
        # Wait for recovery
        await asyncio.sleep(1.1)
        
        # Trigger state check
        async def probe():
            return "probe"
        
        result = await cb.call(probe)
        
        assert result == "probe"
        # Should be half-open or closed now
        assert cb.state in (CircuitState.HALF_OPEN, CircuitState.CLOSED)
    
    @pytest.mark.asyncio
    async def test_half_open_closes_on_success(self, cb):
        """Circuit closes after successes in half-open."""
        # Force to half-open
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(1.1)
        
        async def success():
            return "ok"
        
        # Two successes should close the circuit
        await cb.call(success)
        await cb.call(success)
        
        assert cb.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_half_open_reopens_on_failure(self, cb):
        """Circuit reopens after failure in half-open."""
        # Force to half-open
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(1.1)
        
        async def fail():
            raise TimeoutError("Still failing")
        
        # First call triggers half-open check
        with pytest.raises(TimeoutError):
            await cb.call(fail)
        
        assert cb.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_manual_reset(self, cb):
        """Manual reset closes the circuit."""
        # Force open
        for _ in range(3):
            await cb.record_failure()
        
        assert cb.is_open
        
        await cb.reset()
        
        assert cb.is_closed
        assert cb.failure_count == 0
    
    @pytest.mark.asyncio
    async def test_get_status(self, cb):
        """get_status returns complete information."""
        status = cb.get_status()
        
        assert status["name"] == "test-cb"
        assert status["state"] == "closed"
        assert "failure_count" in status
        assert "config" in status


class TestCircuitBreakerDecorator:
    """Test @circuit_breaker decorator."""
    
    @pytest.mark.asyncio
    async def test_decorator_wraps_function(self):
        """Decorator preserves function behavior."""
        @circuit_breaker("decorator-test")
        async def my_function(x, y):
            return x + y
        
        result = await my_function(1, 2)
        
        assert result == 3
    
    @pytest.mark.asyncio
    async def test_decorator_exposes_circuit_breaker(self):
        """Decorated function has circuit_breaker attribute."""
        @circuit_breaker("exposed-cb")
        async def my_function():
            pass
        
        assert hasattr(my_function, "circuit_breaker")
        assert my_function.circuit_breaker.name == "exposed-cb"


# ============================================================================
# RETRY TESTS
# ============================================================================

class TestRetryConfig:
    """Test RetryConfig defaults."""
    
    def test_default_values(self):
        config = RetryConfig()
        
        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.exponential_base == 2.0


class TestCalculateDelay:
    """Test exponential backoff calculation."""
    
    def test_first_retry_uses_base_delay(self):
        config = RetryConfig(base_delay=1.0, jitter=0.0)
        
        delay = calculate_delay(0, config)
        
        assert delay == 1.0
    
    def test_exponential_increase(self):
        config = RetryConfig(base_delay=1.0, exponential_base=2.0, jitter=0.0)
        
        delays = [calculate_delay(i, config) for i in range(4)]
        
        assert delays == [1.0, 2.0, 4.0, 8.0]
    
    def test_respects_max_delay(self):
        config = RetryConfig(
            base_delay=1.0,
            max_delay=5.0,
            exponential_base=2.0,
            jitter=0.0,
        )
        
        delay = calculate_delay(10, config)  # Would be 1024 without cap
        
        assert delay == 5.0


class TestWithRetryDecorator:
    """Test @with_retry decorator."""
    
    @pytest.mark.asyncio
    async def test_returns_on_success(self):
        """Successful call returns immediately."""
        call_count = 0
        
        @with_retry(max_retries=3)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = await succeed()
        
        assert result == "success"
        assert call_count == 1
    
    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        """TimeoutError triggers retry."""
        call_count = 0
        
        @with_retry(config=RetryConfig(
            max_retries=3,
            base_delay=0.01,  # Fast for testing
        ))
        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("Transient failure")
            return "recovered"
        
        result = await fail_then_succeed()
        
        assert result == "recovered"
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_raises_after_exhaustion(self):
        """Raises after max retries exhausted."""
        call_count = 0
        
        @with_retry(config=RetryConfig(
            max_retries=2,
            base_delay=0.01,
        ))
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise TimeoutError("Persistent failure")
        
        with pytest.raises(TimeoutError):
            await always_fail()
        
        assert call_count == 3  # Initial + 2 retries
    
    @pytest.mark.asyncio
    async def test_skips_retry_on_circuit_open(self):
        """CircuitOpenError is not retried."""
        call_count = 0
        
        @with_retry(max_retries=3)
        async def circuit_fails():
            nonlocal call_count
            call_count += 1
            raise CircuitOpenError("test", 60)
        
        with pytest.raises(CircuitOpenError):
            await circuit_fails()
        
        assert call_count == 1  # No retries
    
    @pytest.mark.asyncio
    async def test_non_retryable_exception_not_retried(self):
        """Exceptions not in retryable list are not retried."""
        call_count = 0
        
        @with_retry(max_retries=3)
        async def value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("Not retryable")
        
        with pytest.raises(ValueError):
            await value_error()
        
        assert call_count == 1


class TestRetryAsync:
    """Test retry_async function."""
    
    @pytest.mark.asyncio
    async def test_functional_retry(self):
        """retry_async provides functional retry pattern."""
        call_count = 0
        
        async def my_func(x):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TimeoutError()
            return x * 2
        
        result = await retry_async(
            my_func,
            RetryConfig(max_retries=3, base_delay=0.01),
            5,
        )
        
        assert result == 10
        assert call_count == 2


# ============================================================================
# RATE LIMITER TESTS
# ============================================================================

class TestRateLimiterConfig:
    """Test RateLimiterConfig defaults."""
    
    def test_default_values(self):
        config = RateLimiterConfig()
        
        assert config.rate == 10.0
        assert config.burst == 20
        assert config.wait_timeout == 30.0


class TestTokenBucketLimiter:
    """Test token bucket rate limiter."""
    
    @pytest.fixture
    def limiter(self):
        """Create a fast limiter for testing."""
        return TokenBucketLimiter(
            name="test-limiter",
            config=RateLimiterConfig(
                rate=100.0,  # Fast refill
                burst=5,
                wait_timeout=1.0,
            ),
        )
    
    @pytest.mark.asyncio
    async def test_starts_with_full_bucket(self, limiter):
        """Limiter starts with burst capacity."""
        assert limiter.available_tokens == 5.0
    
    @pytest.mark.asyncio
    async def test_acquire_decrements_token(self, limiter):
        """acquire() decrements available tokens."""
        initial = limiter.available_tokens
        
        await limiter.acquire()
        
        assert limiter.available_tokens < initial
    
    @pytest.mark.asyncio
    async def test_rapid_acquire_uses_burst(self, limiter):
        """Burst capacity allows rapid acquisition."""
        # Should be able to acquire 5 times rapidly
        for _ in range(5):
            await limiter.acquire()
    
    @pytest.mark.asyncio
    async def test_refills_over_time(self, limiter):
        """Tokens refill based on rate."""
        # Drain bucket
        for _ in range(5):
            await limiter.acquire()
        
        # Wait for refill (100/sec = 1 token per 10ms)
        await asyncio.sleep(0.05)
        
        # Should have tokens again
        await limiter.acquire()  # Should not raise
    
    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        """Raises RateLimitError when timeout exceeded."""
        limiter = TokenBucketLimiter(
            name="slow-limiter",
            config=RateLimiterConfig(
                rate=0.1,  # Very slow
                burst=1,
                wait_timeout=0.1,
            ),
        )
        
        # Use the one token
        await limiter.acquire()
        
        # Next acquire should timeout
        with pytest.raises(RateLimitError):
            await limiter.acquire()
    
    @pytest.mark.asyncio
    async def test_try_acquire_non_blocking(self, limiter):
        """try_acquire() doesn't block."""
        # Should succeed with full bucket
        assert limiter.try_acquire() is True
        
        # Drain bucket
        for _ in range(5):
            await limiter.acquire()
        
        # Should fail without blocking (approximately)
        assert limiter.try_acquire() is False
    
    @pytest.mark.asyncio
    async def test_reset(self, limiter):
        """reset() restores full capacity."""
        # Drain bucket
        for _ in range(5):
            await limiter.acquire()
        
        await limiter.reset()
        
        assert limiter.available_tokens == 5.0
    
    @pytest.mark.asyncio
    async def test_get_status(self, limiter):
        """get_status() returns complete info."""
        status = limiter.get_status()
        
        assert status["name"] == "test-limiter"
        assert "available_tokens" in status
        assert status["rate"] == 100.0
        assert status["burst"] == 5


class TestRateLimitDecorator:
    """Test @rate_limit decorator."""
    
    @pytest.mark.asyncio
    async def test_decorator_applies_limiting(self):
        """Decorator rate limits the function."""
        @rate_limit("decorator-limit", RateLimiterConfig(rate=100, burst=2))
        async def limited():
            return "ok"
        
        # First two calls should be fast
        await limited()
        await limited()
        
        # Third call waits for refill
        start = time.time()
        await limited()
        elapsed = time.time() - start
        
        # Should have waited some amount
        assert elapsed > 0
    
    @pytest.mark.asyncio
    async def test_decorator_exposes_limiter(self):
        """Decorated function has rate_limiter attribute."""
        @rate_limit("exposed-limit")
        async def my_func():
            pass
        
        assert hasattr(my_func, "rate_limiter")
        assert my_func.rate_limiter.name == "exposed-limit"


# ============================================================================
# HEALTH CHECKER TESTS
# ============================================================================

class TestHealthStatus:
    """Test HealthStatus enum."""
    
    def test_status_values(self):
        assert HealthStatus.OK.value == "ok"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.DOWN.value == "down"
        assert HealthStatus.UNKNOWN.value == "unknown"


class TestHealthCheckResult:
    """Test HealthCheckResult dataclass."""
    
    def test_to_dict(self):
        result = HealthCheckResult(
            status=HealthStatus.OK,
            response_time_ms=150,
            structure_changed=False,
            current_hash="abc123",
            baseline_hash="abc123",
        )
        
        d = result.to_dict()
        
        assert d["status"] == "ok"
        assert d["response_time_ms"] == 150
        assert d["structure_changed"] is False


class TestHealthChecker:
    """Test health checker functionality."""
    
    @pytest.fixture
    def checker(self):
        """Create a health checker."""
        return HealthChecker(timeout=5.0)
    
    @pytest.mark.asyncio
    async def test_check_returns_result(self, checker):
        """check() returns a HealthCheckResult."""
        # Mock httpx to avoid real network calls
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "<html><form><input name='rut'></form></html>"
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            result = await checker.check()
        
        assert isinstance(result, HealthCheckResult)
        assert result.status in (HealthStatus.OK, HealthStatus.DEGRADED)
    
    @pytest.mark.asyncio
    async def test_check_sets_baseline(self, checker):
        """First check sets baseline hash."""
        assert checker.baseline_hash is None
        
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "<html><form><input name='rut'></form></html>"
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            await checker.check()
        
        assert checker.baseline_hash is not None
    
    @pytest.mark.asyncio
    async def test_detects_structure_change(self, checker):
        """Detects when HTML structure changes."""
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            # First check - establish baseline
            mock_response1 = MagicMock()
            mock_response1.status_code = 200
            mock_response1.text = "<html><form><input name='rut'></form></html>"
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response1
            )
            
            await checker.check()
            
            # Second check - different structure
            mock_response2 = MagicMock()
            mock_response2.status_code = 200
            mock_response2.text = "<html><div class='new-structure'></div></html>"
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response2
            )
            
            result = await checker.check()
        
        assert result.structure_changed is True
    
    @pytest.mark.asyncio
    async def test_handles_timeout(self, checker):
        """Handles request timeout gracefully."""
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            
            result = await checker.check()
        
        assert result.status == HealthStatus.DOWN
        assert "timeout" in result.error.lower()
    
    @pytest.mark.asyncio
    async def test_handles_connection_error(self, checker):
        """Handles connection errors gracefully."""
        import httpx
        
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            
            result = await checker.check()
        
        assert result.status == HealthStatus.DOWN
        assert result.error is not None
    
    @pytest.mark.asyncio
    async def test_update_baseline(self, checker):
        """update_baseline() updates the hash."""
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "<html><form><input name='rut'></form></html>"
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            await checker.check()
            original_baseline = checker.baseline_hash
            
            # Change structure
            mock_response.text = "<html><div class='new'></div></html>"
            await checker.check()
            
            # Update baseline
            checker.update_baseline()
        
        assert checker.baseline_hash != original_baseline
    
    @pytest.mark.asyncio
    async def test_failure_callback(self, checker):
        """Failure callback is called on DOWN status."""
        callback_called = False
        callback_result = None
        
        async def on_failure(result):
            nonlocal callback_called, callback_result
            callback_called = True
            callback_result = result
        
        checker.on_failure(on_failure)
        
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            
            await checker.check()
        
        assert callback_called is True
        assert callback_result.status == HealthStatus.DOWN
    
    @pytest.mark.asyncio
    async def test_structure_change_callback(self, checker):
        """Structure change callback is called when hash changes."""
        callback_called = False
        
        def on_change(result):
            nonlocal callback_called
            callback_called = True
        
        checker.on_structure_change(on_change)
        
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            # First check
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "<html><form></form></html>"
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            await checker.check()
            
            # Second check with change
            mock_response.text = "<html><div class='different'></div></html>"
            await checker.check()
        
        assert callback_called is True
    
    def test_get_status(self, checker):
        """get_status() returns checker state."""
        status = checker.get_status()
        
        assert status["running"] is False
        assert status["baseline_hash"] is None
        assert status["last_check"] is None


class TestHealthCheckerBackground:
    """Test background monitoring."""
    
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """Can start and stop background monitoring."""
        checker = HealthChecker()
        
        with patch("app.scrapper.pjud.resilience.health.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "<html></html>"
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            await checker.start(interval=0.1)
            assert checker.is_running is True
            
            await asyncio.sleep(0.15)
            
            await checker.stop()
            assert checker.is_running is False


# ============================================================================
# GLOBAL REGISTRY TESTS
# ============================================================================

class TestGlobalRegistries:
    """Test global registry functions."""
    
    def test_get_circuit_breaker_returns_singleton(self):
        """get_circuit_breaker returns same instance."""
        cb1 = get_circuit_breaker("singleton-test")
        cb2 = get_circuit_breaker("singleton-test")
        
        assert cb1 is cb2
    
    def test_get_rate_limiter_returns_singleton(self):
        """get_rate_limiter returns same instance."""
        rl1 = get_rate_limiter("singleton-rate")
        rl2 = get_rate_limiter("singleton-rate")
        
        assert rl1 is rl2
    
    def test_get_health_checker_returns_singleton(self):
        """get_health_checker returns same instance."""
        hc1 = get_health_checker()
        hc2 = get_health_checker()
        
        assert hc1 is hc2
