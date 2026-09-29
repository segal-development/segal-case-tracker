"""Unit tests for app/services/business_days.py.

CRITICAL: días hábiles errors = legal liability.
All expected dates are manually verified against Chilean feriados.

Counting rule under test (art. 66 CPC): SATURDAY IS a día hábil. Only
Sundays and Chilean feriados are excluded. See _is_business_day's docstring
for the legal source.

Feriados used in these tests (verified for the relevant years):
- 2026-09-18 Fiestas Patrias (Friday)
- 2026-09-19 Glorias del Ejército (Saturday feriado — excluded because it is
             a feriado, not because it falls on a Saturday; the "feriado"
             gana" over the "Saturday is hábil" rule)
- 2026-09-21 NOT a feriado in holidays.Chile — Ley 21.169 (Día de la Unidad
             Nacional) is a conditional/optional feriado that the `holidays`
             library does NOT include by default.  Sep 21 is therefore counted
             as a regular business day in all computations.  If the court
             system ever treats it as a feriado, a Slice B override table will
             be required (see deferred work note in deadline_engine.py).
- 2026-04-03 Viernes Santo (Good Friday 2026)
- 2026-04-04 Sábado Santo (Holy Saturday 2026) — also a CL feriado, so it is
             excluded regardless of the "Saturday is hábil" rule.
- 2026-01-01 Año Nuevo
- 2025-01-01 Año Nuevo
"""

from datetime import date

import pytest

from app.services.business_days import (
    _is_business_day,
    add_business_days,
    count_business_days_remaining,
)


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
        """APELACION_5D: 5 días hábiles from 2026-06-16 (Tuesday) → 2026-06-22.

        Spec scenario: trigger_date=2026-06-16, no intervening holiday.
        Count: Wed 17(1), Thu 18(2), Fri 19(3), Sat 20(4, hábil), [Sun 21 skip],
               Mon 22(5). = 5 ✓
        """
        result = add_business_days(date(2026, 6, 16), 5)
        assert result == date(2026, 6, 22)

    def test_traslado_4d_crosses_weekend(self) -> None:
        """TRASLADO_EJECUTANTE_4D: 4 días hábiles from 2026-07-01 (Wed) → 2026-07-06.

        Spec scenario: Saturday counts as hábil, only Sunday is skipped.
        Count: Thu 02(1), Fri 03(2), Sat 04(3, hábil), [Sun 05 skip], Mon 06(4). = 4 ✓
        """
        result = add_business_days(date(2026, 7, 1), 4)
        assert result == date(2026, 7, 6)

    def test_excepciones_8d_crosses_fiestas_patrias(self) -> None:
        """EXCEPCIONES_8D: 8 días hábiles from 2026-09-10 (Thu) → 2026-09-22.

        Spec scenario: Fiestas Patrias (Sep 18, Friday) excluded.
        Sep 19 is a Saturday FERIADO (Glorias del Ejército) — excluded because
        it is a feriado, not because it is a Saturday.
        Count: Fri 11(1), Sat 12(2, hábil), [Sun 13 skip],
               Mon 14(3), Tue 15(4), Wed 16(5), Thu 17(6),
               [Fri 18 FERIADO], [Sat 19 FERIADO], [Sun 20 skip],
               Mon 21(7), Tue 22(8).
        Days counted: 11,12,14,15,16,17,21,22 = 8 ✓
        """
        result = add_business_days(date(2026, 9, 10), 8)
        assert result == date(2026, 9, 22)

    def test_termino_probatorio_10d_crosses_semana_santa(self) -> None:
        """TERMINO_PROBATORIO_10D: 10 días hábiles from 2026-03-24 (Tue) → 2026-04-07.

        Viernes Santo (Apr 3, Friday) and Sábado Santo (Apr 4, Saturday) are
        BOTH CL feriados, so Apr 4 is excluded despite being a Saturday.
        Count: Wed 25(1), Thu 26(2), Fri 27(3), Sat 28(4, hábil), [Sun 29 skip],
               Mon 30(5), Tue 31(6), Wed Apr 1(7), Thu Apr 2(8),
               [Fri Apr 3 VIERNES SANTO], [Sat Apr 4 SÁBADO SANTO], [Sun Apr 5 skip],
               Mon Apr 6(9), Tue Apr 7(10).
        Days counted: 25,26,27,28,30,31,Apr1,Apr2,Apr6,Apr7 = 10 ✓
        """
        result = add_business_days(date(2026, 3, 24), 10)
        assert result == date(2026, 4, 7)

    def test_sentencia_10d_standard(self) -> None:
        """SENTENCIA_10D: 10 días hábiles from 2026-05-04 (Mon) → 2026-05-15.

        No feriados in the period.
        Count: Tue 5(1), Wed 6(2), Thu 7(3), Fri 8(4), Sat 9(5, hábil),
               [Sun 10 skip], Mon 11(6), Tue 12(7), Wed 13(8), Thu 14(9),
               Fri 15(10). = 10 ✓
        """
        result = add_business_days(date(2026, 5, 4), 10)
        assert result == date(2026, 5, 15)

    def test_observaciones_6d_crosses_glorias_navales(self) -> None:
        """OBSERVACIONES_PRUEBA_6D: 6 días hábiles from 2026-05-18 (Mon) → 2026-05-26.

        May 21 (Thursday) = Glorias Navales (feriado).
        Count: Tue 19(1), Wed 20(2), [Thu 21 FERIADO], Fri 22(3),
               Sat 23(4, hábil), [Sun 24 skip], Mon 25(5), Tue 26(6). = 6 ✓
        """
        result = add_business_days(date(2026, 5, 18), 6)
        assert result == date(2026, 5, 26)

    def test_year_boundary_new_year(self) -> None:
        """Correctly skips Jan 1 across the year boundary."""
        # Start: 2025-12-30 (Tuesday), 3 days.
        # Count: Wed Dec 31(1), [Thu Jan 1 FERIADO], Fri Jan 2(2),
        #        Sat Jan 3(3, hábil). = 3 ✓
        result = add_business_days(date(2025, 12, 30), 3)
        assert result == date(2026, 1, 3)

    def test_weekend_only_skip(self) -> None:
        """2 days starting Friday — Saturday counts, Sunday is skipped."""
        # Start: 2026-06-12 (Fri), count: Sat 13(1, hábil), [Sun 14 skip],
        # Mon 15(2). = 2 ✓
        result = add_business_days(date(2026, 6, 12), 2)
        assert result == date(2026, 6, 15)

    @pytest.mark.parametrize(
        "n,expected",
        [
            # 2026-07-01 is Wed; Sat 07-04 is hábil, Sun 07-05 is skipped.
            (4, date(2026, 7, 6)),   # TRASLADO: Thu02,Fri03,Sat04,Mon06
            (5, date(2026, 7, 7)),   # APELACION (+1): ...,Tue07
            (8, date(2026, 7, 10)),  # EXCEPCIONES: ...,Wed08,Thu09,Fri10
            (10, date(2026, 7, 13)), # TERMINO_PROB: ...,Sat11,Mon13
        ],
    )
    def test_all_deadline_types_from_same_start(
        self, n: int, expected: date
    ) -> None:
        """All deadline day counts produce distinct expected dates from 2026-07-01."""
        assert add_business_days(date(2026, 7, 1), n) == expected


# ---------------------------------------------------------------------------
# Art. 66 CPC regression guard: Saturday is hábil, Sunday and feriados are not.
# ---------------------------------------------------------------------------


class TestSaturdayIsBusinessDay:
    """Pins the art. 66 CPC rule so it cannot regress silently.

    Without these tests, a future reader could "fix" _is_business_day back
    to excluding Saturday, reintroducing the bug this change corrects.
    """

    def test_saturday_without_feriado_is_dia_habil(self) -> None:
        """2026-06-13 is a Saturday and NOT a Chilean feriado → hábil."""
        assert _is_business_day(date(2026, 6, 13)) is True

    def test_sunday_is_never_dia_habil(self) -> None:
        """2026-06-14 is a Sunday → inhábil, regardless of feriado status."""
        assert _is_business_day(date(2026, 6, 14)) is False

    def test_weekday_feriado_is_not_dia_habil(self) -> None:
        """2026-09-18 (Friday) = Fiestas Patrias → inhábil."""
        assert _is_business_day(date(2026, 9, 18)) is False

    def test_saturday_feriado_is_not_dia_habil(self) -> None:
        """2026-09-19 (Saturday) = Glorias del Ejército.

        The feriado wins: this date is inhábil because it is a feriado,
        not because it falls on a Saturday.
        """
        assert _is_business_day(date(2026, 9, 19)) is False

    def test_add_business_days_one_day_lands_on_saturday(self) -> None:
        """Friday 2026-06-12 + 1 día hábil = Saturday 2026-06-13.

        Saturday must be reachable as day 1: it counts as hábil.
        """
        result = add_business_days(date(2026, 6, 12), 1)
        assert result == date(2026, 6, 13)

    def test_add_business_days_crosses_weekend_counting_saturday(self) -> None:
        """Deadline crossing a weekend counts Saturday, skips Sunday.

        Start Fri 2026-06-12 (day 0), n=3:
        Sat 13(1, hábil), [Sun 14 skip], Mon 15(2), Tue 16(3). = 3 ✓
        """
        result = add_business_days(date(2026, 6, 12), 3)
        assert result == date(2026, 6, 16)

    def test_count_business_days_remaining_counts_saturday(self) -> None:
        """A due date that falls on the very next Saturday is 1 día hábil away."""
        today = date(2026, 6, 12)  # Friday
        due = date(2026, 6, 13)  # Saturday — counts as hábil
        assert count_business_days_remaining(due, today) == 1


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
