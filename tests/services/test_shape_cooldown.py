"""Tests for the station-wide Shape/TSPD cooldown tracker.

Covers app.services.shape_cooldown.ShapeCooldown: exponential backoff on
trip(), counter reset on clear(), and active()/remaining() against a fully
controlled (mocked) clock — no real sleeps.
"""

from app.services.shape_cooldown import ShapeCooldown


class _FakeClock:
    """Deterministic, manually-advanced clock for testing ShapeCooldown."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestShapeCooldownTrip:
    def test_first_trip_uses_base_duration(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)

        duration = cooldown.trip()

        assert duration == 300.0
        assert cooldown.consecutive == 1

    def test_consecutive_trips_grow_exponentially(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)

        first = cooldown.trip()
        second = cooldown.trip()

        assert first == 300.0
        assert second == 600.0
        assert cooldown.consecutive == 2

    def test_trip_duration_capped_at_max(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)

        cooldown.trip()  # 300, consecutive -> 1
        cooldown.trip()  # 600, consecutive -> 2
        third = cooldown.trip()  # would be 1200, capped to 900

        assert third == 900.0

    def test_trip_sets_active_until_from_current_clock(self):
        clock = _FakeClock(start=1000.0)
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)

        cooldown.trip()

        assert cooldown.active() is True
        assert cooldown.remaining() == 300.0


class TestShapeCooldownClear:
    def test_clear_resets_consecutive_counter(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)
        cooldown.trip()
        cooldown.trip()
        assert cooldown.consecutive == 2

        cooldown.clear()

        assert cooldown.consecutive == 0

    def test_next_trip_after_clear_uses_base_duration_again(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)
        cooldown.trip()
        cooldown.trip()
        cooldown.clear()

        duration = cooldown.trip()

        assert duration == 300.0


class TestShapeCooldownActiveRemaining:
    def test_not_active_before_any_trip(self):
        clock = _FakeClock(start=500.0)
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)

        assert cooldown.active() is False
        assert cooldown.remaining() == 0.0

    def test_active_true_within_window_false_after_expiry(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)
        cooldown.trip()

        clock.advance(299.0)
        assert cooldown.active() is True

        clock.advance(2.0)  # total 301s elapsed, past the 300s window
        assert cooldown.active() is False

    def test_remaining_decreases_as_clock_advances(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)
        cooldown.trip()

        clock.advance(100.0)

        assert cooldown.remaining() == 200.0

    def test_remaining_never_negative_after_expiry(self):
        clock = _FakeClock()
        cooldown = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=clock)
        cooldown.trip()

        clock.advance(1000.0)

        assert cooldown.remaining() == 0.0
