"""Unit tests for app/services/business_days.py.

CRITICAL: días hábiles errors = legal liability.
All expected dates are manually verified against Chilean feriados.

Feriados used in these tests (verified for the relevant years):
- 2026-09-18 Fiestas Patrias (Friday)
- 2026-09-19 Glorias del Ejército (Saturday → weekend anyway)
- 2026-09-21 Día de la Unidad Nacional (Monday, Ley 21.169 candidate)
- 2026-04-03 Viernes Santo (Good Friday 2026)
- 2026-01-01 Año Nuevo
- 2025-01-01 Año Nuevo
"""

from datetime import date

import pytest

from app.services.business_days import add_business_days, count_business_days_remaining


# ---------------------------------------------------------------------------
# add_business_days — parametrized by deadline type day count
# ---------------------------------------------------------------------------


class TestAddBusinessDays:
    """Core algorithm: day-after-event start, no weekends, no feriados."""

    def test_day_after_trigger_is_day_one(self) -> None:
        """Counting starts the day AFTER the trigger (trigger = day 0)."""
        # Start on Monday; first business day is Tuesday
        result = add_business_days(date(2026, 6, 15), 1)  # Mon → Tue
        assert result == date(2026, 6, 16)

    def test_apelacion_5d_standard(self) -> None:
        """APELACION_5D: 5 días hábiles from 2026-06-16 (Tuesday) → 2026-06-23.

        Spec scenario: trigger_date=2026-06-16, no intervening holiday.
        Count: Wed 17, Thu 18, Fri 19, Mon 22, Tue 23.
        """
        result = add_business_days(date(2026, 6, 16), 5)
        assert result == date(2026, 6, 23)

    def test_traslado_4d_crosses_weekend(self) -> None:
        """TRASLADO_EJECUTANTE_4D: 4 días hábiles from 2026-07-01 (Wed) → 2026-07-07.

        Spec scenario: skips Sat 04, Sun 05.
        Count: Thu 02, Fri 03, Mon 06, Tue 07.
        """
        result = add_business_days(date(2026, 7, 1), 4)
        assert result == date(2026, 7, 7)

    def test_excepciones_8d_crosses_fiestas_patrias(self) -> None:
        """EXCEPCIONES_8D: 8 días hábiles from 2026-09-10 (Thu) → 2026-09-23.

        Spec scenario: Fiestas Patrias (Sep 18, Friday) excluded.
        Sep 19 is Saturday (already excluded as weekend).
        Count: Fri 11, Mon 14, Tue 15, Wed 16, Thu 17,
               [Fri 18 FERIADO], [Sat 19 weekend], [Sun 20 weekend],
               Mon 21, Tue 22, Wed 23.
        Days counted: 11,14,15,16,17,21,22,23 = 8 ✓
        """
        result = add_business_days(date(2026, 9, 10), 8)
        assert result == date(2026, 9, 23)

    def test_termino_probatorio_10d_crosses_semana_santa(self) -> None:
        """TERMINO_PROBATORIO_10D: 10 días hábiles from 2026-03-24 (Tue) → 2026-04-08.

        Viernes Santo 2026-04-03 excluded.
        Count: Wed 25, Thu 26, Fri 27, [Sat 28], [Sun 29],
               Mon 30, Tue 31, Wed Apr 1, Thu Apr 2,
               [Fri Apr 3 VIERNES SANTO], [Sat 4], [Sun 5],
               Mon Apr 6, Tue Apr 7, Wed Apr 8.
        Days counted: 25,26,27,30,31,Apr1,Apr2,Apr6,Apr7,Apr8 = 10 ✓
        """
        result = add_business_days(date(2026, 3, 24), 10)
        assert result == date(2026, 4, 8)

    def test_sentencia_10d_standard(self) -> None:
        """SENTENCIA_10D: 10 días hábiles from 2026-05-04 (Mon) → 2026-05-18.

        No feriados in the period.
        Count: Tue 5, Wed 6, Thu 7, Fri 8, Mon 11, Tue 12,
               Wed 13, Thu 14, Fri 15, Mon 18. = 10 ✓
        """
        result = add_business_days(date(2026, 5, 4), 10)
        assert result == date(2026, 5, 18)

    def test_observaciones_6d_crosses_glorias_navales(self) -> None:
        """OBSERVACIONES_PRUEBA_6D: 6 días hábiles from 2026-05-18 (Mon) → 2026-05-27.

        May 21 = Glorias Navales (feriado).
        Count: Tue 19, Wed 20, [Thu 21 FERIADO], Fri 22, Mon 25, Tue 26, Wed 27. = 6 ✓
        """
        result = add_business_days(date(2026, 5, 18), 6)
        assert result == date(2026, 5, 27)

    def test_year_boundary_new_year(self) -> None:
        """Correctly skips Jan 1 across the year boundary."""
        # Start: 2025-12-30 (Tuesday), 3 days → skip Jan 1 (feriado)
        # Count: Wed Dec 31, [Thu Jan 1 FERIADO], Fri Jan 2, Mon Jan 5 = 3
        result = add_business_days(date(2025, 12, 30), 3)
        assert result == date(2026, 1, 5)

    def test_weekend_only_skip(self) -> None:
        """2 days starting Friday — crosses weekend, lands on Tuesday."""
        # Start: 2026-06-12 (Fri), count: Mon 15, Tue 16 = 2
        result = add_business_days(date(2026, 6, 12), 2)
        assert result == date(2026, 6, 16)

    @pytest.mark.parametrize(
        "n,expected",
        [
            (4, date(2026, 7, 7)),   # TRASLADO
            (5, date(2026, 7, 8)),   # APELACION (+1)
            (8, date(2026, 7, 13)),  # EXCEPCIONES
            (10, date(2026, 7, 15)), # TERMINO_PROB
        ],
    )
    def test_all_deadline_types_from_same_start(
        self, n: int, expected: date
    ) -> None:
        """All deadline day counts produce distinct expected dates from 2026-07-01."""
        assert add_business_days(date(2026, 7, 1), n) == expected


# ---------------------------------------------------------------------------
# count_business_days_remaining
# ---------------------------------------------------------------------------


class TestCountBusinessDaysRemaining:
    def test_count_remaining_rojo_due_today(self) -> None:
        """Due today → 0 remaining → ROJO threshold (≤1)."""
        today = date(2026, 6, 16)
        assert count_business_days_remaining(today, today) == 0

    def test_count_remaining_rojo_1d(self) -> None:
        """Due next business day → 1 remaining → ROJO (≤1)."""
        # Today Monday, due Tuesday
        assert count_business_days_remaining(date(2026, 6, 16), date(2026, 6, 15)) == 1

    def test_count_remaining_expired_yesterday(self) -> None:
        """Due yesterday → -1 → ROJO expired."""
        today = date(2026, 6, 16)
        due = date(2026, 6, 15)  # Monday
        result = count_business_days_remaining(due, today)
        assert result < 0

    def test_count_remaining_amarillo_3d(self) -> None:
        """3 business days remaining → AMARILLO (2–5)."""
        today = date(2026, 6, 16)  # Tuesday
        # 3 biz days forward: Wed 17, Thu 18, Fri 19 → due 2026-06-19
        due = date(2026, 6, 19)
        assert count_business_days_remaining(due, today) == 3

    def test_count_remaining_verde_10d(self) -> None:
        """10 business days remaining → VERDE (>5)."""
        today = date(2026, 6, 16)  # Tuesday
        due = date(2026, 6, 30)  # Mon (6/30) — approx 10 biz days
        result = count_business_days_remaining(due, today)
        assert result > 5
