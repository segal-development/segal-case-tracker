"""Unit tests for pjud_action_limiter — no network required."""

import pytest

from app.scrapper.pjud.resilience.rate_limiter import (
    TokenBucketLimiter,
    pjud_action_limiter,
)


def test_pjud_action_limiter_returns_token_bucket_limiter():
    limiter = pjud_action_limiter("detail")
    assert isinstance(limiter, TokenBucketLimiter)


def test_different_actions_return_different_limiter_instances():
    detail_limiter = pjud_action_limiter("detail")
    list_limiter = pjud_action_limiter("list")
    login_limiter = pjud_action_limiter("login")
    document_limiter = pjud_action_limiter("document")

    # Each action gets its own named bucket.
    assert detail_limiter.name == "pjud:detail"
    assert list_limiter.name == "pjud:list"
    assert login_limiter.name == "pjud:login"
    assert document_limiter.name == "pjud:document"

    # Registry reuses the same object for the same name.
    assert pjud_action_limiter("detail") is detail_limiter
    assert pjud_action_limiter("list") is list_limiter


@pytest.mark.asyncio
async def test_acquire_within_burst_is_instant():
    """First acquire on a fresh bucket should succeed immediately (True)."""
    # Use a fresh limiter name so the global registry doesn't bleed state.
    from app.scrapper.pjud.resilience.rate_limiter import get_rate_limiter, RateLimiterConfig

    limiter = get_rate_limiter(
        "pjud:test_burst",
        RateLimiterConfig(rate=0.5, burst=5, wait_timeout=5.0),
    )
    await limiter.reset()  # ensure full bucket

    result = await limiter.acquire()
    assert result is True


@pytest.mark.asyncio
async def test_detail_action_acquire_returns_true_within_burst():
    """pjud_action_limiter('detail').acquire() returns True on the first call."""
    limiter = pjud_action_limiter("detail")
    await limiter.reset()  # fill the bucket

    result = await limiter.acquire()
    assert result is True


@pytest.mark.asyncio
async def test_unknown_action_falls_back_without_crashing():
    """An unknown action string should not crash — falls back to a safe default."""
    limiter = pjud_action_limiter("unknown_action_xyz")
    assert isinstance(limiter, TokenBucketLimiter)
    assert limiter.name == "pjud:unknown_action_xyz"

    await limiter.reset()
    result = await limiter.acquire()
    assert result is True
