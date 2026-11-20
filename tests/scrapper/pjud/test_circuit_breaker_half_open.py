"""Half-open slot leaks must never leave the breaker rejecting forever."""

import asyncio
import importlib

import pytest

from app.scrapper.pjud.exceptions import CircuitOpenError
from app.scrapper.pjud.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)

cbmod = importlib.import_module("app.scrapper.pjud.resilience.circuit_breaker")


class Expired(Exception):
    pass


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cbmod.time, "time", c)
    return c


def make():
    config = CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout=60,
        half_open_max_calls=2,
        success_threshold=2,
        ignored_exceptions=(Expired,),
    )
    return CircuitBreaker(name="pjud-detail", config=config)


def stick_half_open(cb, clock):
    """Simulate admitted probes that never reported back."""
    cb._state = CircuitState.HALF_OPEN
    cb._half_open_calls = cb.config.half_open_max_calls
    cb._half_open_since = clock.now


async def _boom():
    raise RuntimeError("down")


async def _ok():
    return "ok"


async def _open(cb, clock):
    for _ in range(cb.config.failure_threshold):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.state == CircuitState.OPEN
    clock.now += cb.config.recovery_timeout


async def test_ignored_exception_in_half_open_releases_slot(clock):
    cb = make()
    await _open(cb, clock)

    async def expired():
        raise Expired()

    for _ in range(5):  # more than half_open_max_calls
        with pytest.raises(Expired):
            await cb.call(expired)
    assert cb.state == CircuitState.HALF_OPEN
    assert await cb.call(_ok) == "ok"


async def test_cancelled_half_open_call_releases_slot_without_failure(clock):
    cb = make()
    await _open(cb, clock)
    failures = cb.failure_count

    async def cancelled():
        raise asyncio.CancelledError()

    for _ in range(5):  # more than half_open_max_calls
        with pytest.raises(asyncio.CancelledError):
            await cb.call(cancelled)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb._half_open_calls == 0
    assert cb.failure_count == failures
    assert await cb.call(_ok) == "ok"


async def test_cancellation_in_closed_state_is_not_a_failure(clock):
    cb = make()

    async def cancelled():
        raise asyncio.CancelledError()

    for _ in range(10):
        with pytest.raises(asyncio.CancelledError):
            await cb.call(cancelled)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


async def test_wait_for_timeouts_in_half_open_release_slots(clock):
    cb = make()
    await _open(cb, clock)

    async def slow():
        await asyncio.sleep(10)

    for _ in range(5):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cb.call(slow), timeout=0.01)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb._half_open_calls == 0
    assert await cb.call(_ok) == "ok"


async def test_stale_generation_release_is_noop(clock):
    cb = make()
    await _open(cb, clock)
    assert await cb.call(_ok) == "ok"  # admitted probe, now 1 call in window
    stale = cb._half_open_generation - 1
    await cb._release_slot(True, stale)
    assert cb._half_open_calls == 1


async def test_keyboard_interrupt_in_half_open_releases_slot(clock):
    cb = make()
    await _open(cb, clock)

    async def kb():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        await cb.call(kb)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb._half_open_calls == 0
    assert cb.failure_count == cb.config.failure_threshold


async def test_stuck_slots_expire_after_recovery_timeout(clock):
    cb = make()
    stick_half_open(cb, clock)
    with pytest.raises(CircuitOpenError):
        await cb.call(_ok)
    clock.now += 61
    assert await cb.call(_ok) == "ok"


async def test_half_open_successes_close(clock):
    cb = make()
    await _open(cb, clock)
    assert await cb.call(_ok) == "ok"
    assert cb.state == CircuitState.HALF_OPEN
    assert await cb.call(_ok) == "ok"
    assert cb.state == CircuitState.CLOSED


async def test_half_open_failure_reopens_with_fresh_timer(clock):
    cb = make()
    await _open(cb, clock)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state == CircuitState.OPEN
    assert cb.time_until_recovery == 60
    clock.now += 59
    with pytest.raises(CircuitOpenError):
        await cb.call(_ok)
    clock.now += 1
    assert await cb.call(_ok) == "ok"


async def test_rejection_reports_remaining_time(clock):
    cb = make()
    stick_half_open(cb, clock)
    seen = []
    for step in (0, 10, 20):
        clock.now += step
        with pytest.raises(CircuitOpenError) as ei:
            await cb.call(_ok)
        seen.append(ei.value.recovery_time)
    assert seen == [60, 50, 30]


async def test_nested_circuit_open_error_not_counted_as_failure(clock):
    cb = make()

    async def nested():
        raise CircuitOpenError(competencia="other", recovery_time=5)

    for _ in range(5):
        with pytest.raises(CircuitOpenError):
            await cb.call(nested)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


async def test_closed_failures_still_open_breaker(clock):
    cb = make()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.state == CircuitState.OPEN
