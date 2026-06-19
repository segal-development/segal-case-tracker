"""Tests for resilient_call: retry + circuit breaker integration."""
import asyncio
import pytest
import uuid
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def mock_settings():
    """Provide minimal settings for all tests."""
    settings = MagicMock()
    settings.PJUD_RETRY_MAX_ATTEMPTS = 3
    settings.PJUD_RETRY_BASE_DELAY = 0.0  # no sleep in tests
    settings.PJUD_RETRY_MAX_DELAY = 0.1
    settings.PJUD_CB_FAILURE_THRESHOLD = 3
    settings.PJUD_CB_RECOVERY_TIMEOUT = 60
    with patch("app.config.settings", settings):
        yield settings


async def test_retries_on_transient_then_succeeds():
    """Fails twice with a transient error, succeeds on third call."""
    op = f"test-retry-{uuid.uuid4()}"
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise TimeoutError("transient")
        return "ok"

    from app.scrapper.pjud.resilience.integration import resilient_call
    result = await resilient_call(op, flaky)
    assert result == "ok"
    assert call_count == 3


async def test_raises_after_max_retries():
    """Exhausts all retries and raises the transient error."""
    op = f"test-exhaust-{uuid.uuid4()}"

    async def always_timeout():
        raise TimeoutError("always")

    from app.scrapper.pjud.resilience.integration import resilient_call
    with pytest.raises(TimeoutError):
        await resilient_call(op, always_timeout)


async def test_non_retryable_raises_immediately():
    """Non-retryable error is raised after exactly 1 call."""
    op = f"test-nonretry-{uuid.uuid4()}"
    call_count = 0

    async def bad():
        nonlocal call_count
        call_count += 1
        raise ValueError("not retryable")

    from app.scrapper.pjud.resilience.integration import resilient_call
    with pytest.raises(ValueError):
        await resilient_call(op, bad)
    assert call_count == 1


async def test_circuit_opens_after_threshold():
    """After failure_threshold failures, CB opens and raises CircuitOpenError."""
    op = f"test-cb-{uuid.uuid4()}"

    async def always_fail():
        raise ValueError("hard fail")

    from app.scrapper.pjud.resilience.integration import resilient_call
    from app.scrapper.pjud.exceptions import CircuitOpenError

    # Exhaust the CB (failure_threshold=3, each call fails after max_retries attempts)
    for _ in range(3):
        with pytest.raises((ValueError, CircuitOpenError)):
            await resilient_call(op, always_fail)

    # Now CB should be open
    factory_called = False

    async def should_not_run():
        nonlocal factory_called
        factory_called = True
        return "should not reach"

    with pytest.raises(CircuitOpenError):
        await resilient_call(op, should_not_run)

    assert not factory_called, "Factory must not be called when CB is open"
