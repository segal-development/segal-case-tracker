"""Chilean business day (día hábil) arithmetic.

ADVISORY: outputs are used for legal deadline guidance only.  Always
surface DEADLINE_DISCLAIMER from app.core.deadlines_config to end-users.

Counting convention (CPC):
- The trigger/event date is day 0 (NOT counted).
- The first business day after the event is day 1.
- Only Sundays and Chilean feriados are excluded. Saturday IS a día hábil
  (art. 66 CPC: terms are suspended only on feriados — for this purpose,
  feriados means Sundays and holidays — Saturday is not a feriado).

Feriado source: ``holidays`` library (Chile), pinned to ^0.58 in
pyproject.toml.  The library covers fixed + moveable feriados (Viernes
Santo, Ley 21.169 additions) without manual maintenance.  Unit tests
pin known feriados as a regression guard.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Union

import holidays


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _cl_holidays_cached(year: int) -> holidays.HolidayBase:
    """Cached per-year feriado lookup (avoids repeated library calls)."""
    return holidays.country_holidays("CL", years=year)


def _is_business_day(d: date) -> bool:
    """Return True when *d* is a día hábil (business day) under art. 66 CPC.

    INTENTIONAL: Saturday counts as hábil. Art. 66 CPC suspends judicial
    terms only during "feriados", and for this purpose feriados means
    Sundays and holidays (Dirección Jurídica, "Flujo de excepciones
    dilatorias", confirmado 2026-09-29). Saturday is NOT a feriado in
    Chilean civil procedure, so it must NOT be excluded here. Do not
    "fix" this back to `d.weekday() >= 5` — that reintroduces a bug that
    silently lengthened every deadline. Measured against the QA database
    on 2026-09-29: 779 of 973 active deadlines carried a due_date later
    than the legal one, by up to 13 days.
    """
    if d.weekday() == 6:  # Sunday=6
        return False
    cl = _cl_holidays_cached(d.year)
    return d not in cl


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_business_days(start: date, n: int) -> date:
    """Return the date that is *n* días hábiles after *start*.

    Counting starts the day AFTER *start* (start = day 0).

    Args:
        start: The trigger/event date (day 0 — not counted).
        n: Number of business days to add (must be > 0).

    Returns:
        The due date (day n).

    Raises:
        ValueError: when *n* is not a positive integer.
    """
    if not isinstance(n, int) or n <= 0:
        raise ValueError(f"n must be a positive integer, got {n!r}")

    current = start
    counted = 0
    while counted < n:
        current += timedelta(days=1)
        if _is_business_day(current):
            counted += 1
    return current


def count_business_days_remaining(due_date: date, today: date) -> int:
    """Count business days between *today* and *due_date*.

    Returns:
        Positive integer when *due_date* is in the future.
        0 when *due_date* == *today*.
        Negative integer when *due_date* is in the past (expired).

    The value maps directly to the semáforo thresholds:
        ≤ 1  → ROJO
        2–5  → AMARILLO
        > 5  → VERDE
        (caller checks state == INDETERMINATE first → GRIS)
    """
    if due_date == today:
        return 0

    if due_date > today:
        count = 0
        current = today
        while current < due_date:
            current += timedelta(days=1)
            if _is_business_day(current):
                count += 1
        return count
    else:
        # Expired: count backwards (return negative)
        count = 0
        current = due_date
        while current < today:
            current += timedelta(days=1)
            if _is_business_day(current):
                count += 1
        return -count
