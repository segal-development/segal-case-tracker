"""
Shape/TSPD cooldown tracker for the PJUD scraper.

When PJUD's Shape/TSPD bot-detection challenges the station, it flags the
egress IP's behavioral reputation. This is a station-wide condition, not a
per-session or per-lawyer one: re-authenticating and retrying within seconds
(as we do for a genuine session expiry) only hammers the flagged IP again
during its reputation-cooldown window, deepening the block instead of
recovering from it.

``ShapeCooldown`` tracks that cooldown window with exponential backoff per
consecutive hit. A process-global singleton (mirroring the module-level
circuit breaker registry) is exposed via :func:`get_shape_cooldown` so every
lawyer's sync cycle can check/trip/clear the SAME cooldown — a Shape hit
while processing one lawyer must back off detail work for all of them.
"""

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ShapeCooldown:
    """Tracks a station-wide Shape/TSPD block cooldown.

    ``now_fn`` defaults to ``time.monotonic`` but can be swapped for a
    controllable clock in tests so ``trip()``/``active()``/``remaining()``
    are fully deterministic without real sleeps.
    """

    base_seconds: float = 300.0
    max_seconds: float = 900.0
    now_fn: Callable[[], float] = field(default=time.monotonic)

    consecutive: int = field(default=0, init=False)
    _active_until: float = field(default=0.0, init=False)

    def trip(self) -> float:
        """Record a Shape hit: (re)start the cooldown window.

        Duration grows exponentially with each consecutive hit
        (``base_seconds * 2**consecutive``), capped at ``max_seconds``.
        Returns the duration (seconds) of the cooldown just started.
        """
        duration = min(self.base_seconds * (2 ** self.consecutive), self.max_seconds)
        self.consecutive += 1
        self._active_until = self.now_fn() + duration
        return duration

    def clear(self) -> None:
        """Reset the consecutive-hit counter.

        Call this after any SUCCESSFUL detail fetch — it is the clearest
        signal that Shape is no longer flagging this station.
        """
        self.consecutive = 0

    def active(self) -> bool:
        """Whether a cooldown is currently in effect."""
        return self.now_fn() < self._active_until

    def remaining(self) -> float:
        """Seconds left in the current cooldown window (0 if not active)."""
        return max(0.0, self._active_until - self.now_fn())


# Process-global singleton. Shape flags the egress IP, so this MUST be
# shared across every lawyer's sync cycle rather than scoped per-lawyer or
# per-session.
_shape_cooldown: "ShapeCooldown | None" = None


def get_shape_cooldown() -> ShapeCooldown:
    """Return the process-global ShapeCooldown, creating it from settings
    on first access."""
    global _shape_cooldown
    if _shape_cooldown is None:
        from app.config import settings

        _shape_cooldown = ShapeCooldown(
            base_seconds=float(settings.SHAPE_COOLDOWN_BASE_SECONDS),
            max_seconds=float(settings.SHAPE_COOLDOWN_MAX_SECONDS),
        )
    return _shape_cooldown


def reset_shape_cooldown() -> None:
    """Test helper: drop the singleton so the next ``get_shape_cooldown()``
    call rebuilds it from current settings. Not used in production code."""
    global _shape_cooldown
    _shape_cooldown = None
