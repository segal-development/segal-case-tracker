"""Per-cycle time accounting for the scraping worker.

Observability only: it measures, it never changes what the worker does.

The worker runs one long cycle every N hours. To learn where a cycle's time
goes, the code at the interesting points wraps its work in ``timed("bucket")``.
When no cycle is being measured (the on-demand API endpoints, unit tests) the
wrapper does nothing, so instrumented code needs no extra parameters.

Buckets are EXCLUSIVE: a bucket nested inside another pauses its parent, so
the percentages of one cycle add up to 100% instead of counting the document
download once under "detail" and again under "documents". Time not inside any
bucket is reported as "other".

The accounting assumes the work inside a cycle is sequential, which is how the
worker runs it (one lawyer at a time, one case at a time).
"""

import functools
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterator, List, Optional

# Monotonic clock; tests replace it. Read through the module global at call time.
_clock = time.monotonic


def _now() -> float:
    return _clock()


# Order used when printing the breakdown.
BUCKET_ORDER = (
    "listing",      # PJUD case list pages + upsert of the list
    "login",        # (re)authentication against PJUD
    "detail",       # case detail fetch + movement/entity processing (exclusive of the rest)
    "documents",    # PDF downloads
    "email",        # SMTP delivery of alerts
    "waits",        # deliberate pauses: inter-case delay, retry backoff, inter-unit sleep
    "maintenance",  # work before the lawyer loop: credential scan, Sysgal, health, snapshot
)


class CycleTimer:
    """Accumulates exclusive time per bucket for one worker cycle."""

    def __init__(self) -> None:
        self.started_at: float = _now()
        self.totals: Dict[str, float] = {}
        # Lawyer counters of the cycle, filled in by the scheduler for the summary.
        self.results: Optional[dict] = None
        self._stack: List[str] = []
        self._mark: float = self.started_at

    def _charge(self, now: float) -> None:
        if self._stack:
            bucket = self._stack[-1]
            self.totals[bucket] = self.totals.get(bucket, 0.0) + (now - self._mark)
        self._mark = now

    def push(self, bucket: str) -> None:
        self._charge(_now())
        self._stack.append(bucket)

    def pop(self) -> None:
        self._charge(_now())
        if self._stack:
            self._stack.pop()

    def add(self, bucket: str, seconds: float) -> None:
        """Charge time measured elsewhere (e.g. the pre-loop maintenance block)."""
        self.totals[bucket] = self.totals.get(bucket, 0.0) + max(seconds, 0.0)

    def elapsed(self) -> float:
        return _now() - self.started_at

    def breakdown(self) -> Dict[str, float]:
        """Seconds per bucket plus ``other`` (total minus everything measured)."""
        total = self.elapsed()
        result = {name: self.totals.get(name, 0.0) for name in BUCKET_ORDER}
        for name, value in self.totals.items():
            result.setdefault(name, value)
        result["other"] = max(total - sum(result.values()), 0.0)
        return result


_active: ContextVar[Optional[CycleTimer]] = ContextVar("cycle_timer", default=None)


@contextmanager
def use_timer(timer: CycleTimer) -> Iterator[CycleTimer]:
    """Make *timer* the one ``timed`` charges for the duration of the block."""
    token = _active.set(timer)
    try:
        yield timer
    finally:
        _active.reset(token)


@contextmanager
def timed(bucket: str) -> Iterator[None]:
    """Charge the enclosed time to *bucket* of the active cycle, if any."""
    timer = _active.get()
    if timer is None:
        yield
        return
    timer.push(bucket)
    try:
        yield
    finally:
        timer.pop()


def timed_async(bucket: str):
    """Decorator form of ``timed`` for coroutine functions."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with timed(bucket):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def format_duration(seconds: float) -> str:
    """``42s``, ``3m05s`` or ``2h05m10s`` — compact and unambiguous in a log."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_gap(seconds: float) -> str:
    """Minute-granularity span for gaps between cycles: ``35m``, ``2h05m``."""
    total_minutes = int(round(seconds / 60.0))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_breakdown(timer: CycleTimer) -> str:
    """``listing=1m00s (24%) detail=1m40s (40%) ...`` for the cycle summary line."""
    total = timer.elapsed()
    parts = []
    for name, seconds in timer.breakdown().items():
        pct = (seconds / total * 100.0) if total > 0 else 0.0
        parts.append(f"{name}={format_duration(seconds)} ({pct:.0f}%)")
    return " ".join(parts)
