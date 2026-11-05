"""Pure semaforo levels per deadline (no DB).

Calendar used throughout (trigger = Monday 2026-11-02, no feriado in range):

    day:    1     2     3     4     5     6     7     8
    date:  Tue3  Wed4  Thu5  Fri6  Sat7  Mon9  Tue10 Wed11

Saturday is a working day (art. 66 CPC), Sunday 11-08 is not.
"""

from datetime import date

import pytest

from app.core.semaforo_config import (
    SCALE_EXCEPCIONES_4D,
    SCALE_APELACION_5D,
    SCALE_EXCEPCIONES_8D,
    SCALE_PODER_3D,
    Level,
)
from app.services.business_days import business_days_elapsed
from app.services.deadline_semaforo import assess, confirmed_excepciones_term

TRIGGER = date(2026, 11, 2)
V, A, R = Level.VERDE, Level.AMARILLO, Level.ROJO

DAY_DATE = {
    1: date(2026, 11, 3),
    2: date(2026, 11, 4),
    3: date(2026, 11, 5),
    4: date(2026, 11, 6),
    5: date(2026, 11, 7),
    6: date(2026, 11, 9),
    7: date(2026, 11, 10),
    8: date(2026, 11, 11),
    9: date(2026, 11, 12),
    10: date(2026, 11, 13),
    11: date(2026, 11, 14),
}


class TestBusinessDaysElapsed:
    def test_trigger_day_is_zero(self) -> None:
        assert business_days_elapsed(TRIGGER, TRIGGER) == 0

    def test_counts_saturday_and_skips_sunday(self) -> None:
        assert business_days_elapsed(TRIGGER, date(2026, 11, 7)) == 5  # Sat
        assert business_days_elapsed(TRIGGER, date(2026, 11, 8)) == 5  # Sun adds none
        assert business_days_elapsed(TRIGGER, date(2026, 11, 9)) == 6

    def test_skips_feriados(self) -> None:
        # Sun 10-11 and the feriado Mon 10-12 add nothing: from Sat 10-10,
        # Tue 10-13 is day 1.
        assert business_days_elapsed(date(2026, 10, 10), date(2026, 10, 12)) == 0
        assert business_days_elapsed(date(2026, 10, 10), date(2026, 10, 13)) == 1

    def test_future_trigger_is_zero(self) -> None:
        assert business_days_elapsed(date(2026, 11, 9), TRIGGER) == 0


class TestScaleBoundaries:
    """The day ranges of the specification, one boundary at a time."""

    @pytest.mark.parametrize(
        "day,expected",
        [(1, V), (2, V), (3, V), (4, V), (5, V), (6, A), (7, A), (8, R), (9, None)],
    )
    def test_excepciones_8_days(self, day, expected) -> None:
        assert SCALE_EXCEPCIONES_8D.level_for_day(day) == expected

    @pytest.mark.parametrize(
        "day,expected", [(1, V), (2, V), (3, A), (4, R), (5, None)]
    )
    def test_excepciones_4_days(self, day, expected) -> None:
        assert SCALE_EXCEPCIONES_4D.level_for_day(day) == expected

    @pytest.mark.parametrize("day,expected", [(1, V), (2, A), (3, R), (4, None)])
    def test_ratificacion_3_days(self, day, expected) -> None:
        assert SCALE_PODER_3D.level_for_day(day) == expected

    @pytest.mark.parametrize(
        "day,expected",
        [(1, V), (5, V), (6, A), (9, A), (10, R), (11, None)],
    )
    def test_apelacion_10_days(self, day, expected) -> None:
        # The scale keeps its historical "5d" name; the term is 10 días (art. 189).
        assert SCALE_APELACION_5D.level_for_day(day) == expected

    def test_day_zero_has_no_level_on_the_scale_itself(self) -> None:
        assert SCALE_EXCEPCIONES_8D.level_for_day(0) is None


class TestConfirmedTerm:
    @pytest.mark.parametrize("day,expected", [(1, V), (5, V), (6, A), (7, A), (8, R)])
    def test_confirmed_8_follows_the_8_day_scale(self, day, expected) -> None:
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[day], confirmed_term_days=8)
        assert a.confirmed is True
        assert a.level == expected
        assert a.slot == "8d"
        assert [r.term_days for r in a.readings] == [8]
        assert a.readings[0].due_date == DAY_DATE[8]

    @pytest.mark.parametrize("day,expected", [(1, V), (2, V), (3, A), (4, R)])
    def test_confirmed_4_follows_the_4_day_scale(self, day, expected) -> None:
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[day], confirmed_term_days=4)
        assert a.level == expected
        assert a.slot == "4d"
        assert a.readings[0].due_date == DAY_DATE[4]

    def test_confirmed_term_lapses_after_the_last_day(self) -> None:
        assert assess("excepciones_8d", TRIGGER, DAY_DATE[8], confirmed_term_days=4) is not None
        a = assess("excepciones_8d", TRIGGER, date(2026, 11, 7), confirmed_term_days=4)
        assert a.level is None  # day 5 of a 4-day term: it is over

    def test_a_term_that_is_neither_4_nor_8_is_refused(self) -> None:
        with pytest.raises(ValueError):
            assess("excepciones_8d", TRIGGER, DAY_DATE[2], confirmed_term_days=6)


class TestUnconfirmedTerm:
    """Nothing in any source says which term applies: both readings stay open,
    and the level is the most urgent of the readings still possible."""

    @pytest.mark.parametrize(
        "day,level,slot",
        [
            (1, V, "u4+8"),
            (2, V, "u4+8"),
            (3, A, "u4+8"),
            (4, R, "u4+8"),
            (5, V, "u8"),   # the 4-day reading is over; only the 8-day one is left
            (6, A, "u8"),
            (7, A, "u8"),
            (8, R, "u8"),
        ],
    )
    def test_level_is_the_most_urgent_live_reading(self, day, level, slot) -> None:
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[day])
        assert a.confirmed is False
        assert a.level == level
        assert a.slot == slot

    def test_both_candidate_dates_are_carried(self) -> None:
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[3])
        assert {r.term_days: r.due_date for r in a.readings} == {
            4: DAY_DATE[4],
            8: DAY_DATE[8],
        }

    def test_after_the_4_day_reading_lapses_it_is_still_listed(self) -> None:
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[6])
        four = next(r for r in a.readings if r.term_days == 4)
        assert four.level is None and four.due_date == DAY_DATE[4]

    def test_nothing_is_live_after_day_8(self) -> None:
        a = assess("excepciones_8d", TRIGGER, date(2026, 11, 12))
        assert a.level is None

    def test_no_per_plazo_source_confirms_the_term_today(self, monkeypatch) -> None:
        """lugar_requerimiento exists in no source: until one does, nothing
        confirms the term PER PLAZO. This test is the tripwire: if it starts
        failing somebody wired a source, and the alert copy needs a second look.

        The firm-wide setting is pinned to 0 here on purpose. It is a statement
        about how the firm's cases run, not a per-plazo source, so leaving it at
        its shipping default (8) would make this assertion pass for the wrong
        reason and the tripwire would never fire again.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "SEMAFORO_EXCEPCIONES_TERM_DAYS", 0)
        assert confirmed_excepciones_term(object()) is None


class TestRatificacion:
    @pytest.mark.parametrize("day,expected", [(1, V), (2, A), (3, R)])
    def test_three_day_scale(self, day, expected) -> None:
        a = assess("acreditar_poder_3d", TRIGGER, DAY_DATE[day])
        assert a.confirmed is True
        assert a.level == expected
        assert a.slot == "3d"
        assert a.readings[0].due_date == DAY_DATE[3]

    def test_over_after_day_3(self) -> None:
        assert assess("acreditar_poder_3d", TRIGGER, DAY_DATE[4]).level is None

    def test_a_confirmed_term_argument_is_ignored(self) -> None:
        a = assess("acreditar_poder_3d", TRIGGER, DAY_DATE[2], confirmed_term_days=8)
        assert a.slot == "3d"


class TestApelacion:
    """Art. 189 CPC: ONE known term of 10 business days, so never unconfirmed.

    The "apelacion_5d" identifier and the "5d" slot are historical names.
    """

    @pytest.mark.parametrize(
        "day,expected", [(1, V), (5, V), (6, A), (9, A), (10, R)]
    )
    def test_ten_day_scale(self, day, expected) -> None:
        a = assess("apelacion_5d", TRIGGER, DAY_DATE[day])
        assert a is not None
        assert a.level == expected
        assert a.day == day
        assert a.slot == "5d"
        assert a.readings[0].due_date == DAY_DATE[10]

    def test_over_from_day_11(self) -> None:
        a = assess("apelacion_5d", TRIGGER, DAY_DATE[11])
        assert a is not None
        assert a.level is None
        assert a.readings[0].level is None
        assert a.live_readings == ()

    def test_term_is_always_confirmed_with_exactly_one_reading(self) -> None:
        a = assess("apelacion_5d", TRIGGER, DAY_DATE[2])
        assert a is not None
        assert a.confirmed is True
        assert [r.term_days for r in a.readings] == [10]

    def test_a_confirmed_term_argument_is_ignored(self) -> None:
        a = assess("apelacion_5d", TRIGGER, DAY_DATE[2], confirmed_term_days=8)
        assert a is not None and a.slot == "5d" and a.confirmed is True


class TestEdges:
    def test_the_day_the_term_opens_counts_as_day_one(self) -> None:
        a = assess("excepciones_8d", TRIGGER, TRIGGER, confirmed_term_days=8)
        assert a.day == 1 and a.level == V

    def test_a_sunday_after_the_due_date_is_not_a_live_day(self) -> None:
        # 4-day term ends Fri 11-06; Saturday 11-07 is a working day: over.
        a = assess("excepciones_8d", TRIGGER, date(2026, 11, 7), confirmed_term_days=4)
        assert a.level is None

    def test_unknown_deadline_type_has_no_semaforo(self) -> None:
        assert assess("no_such_deadline", TRIGGER, DAY_DATE[2]) is None


class TestPinnedExcepcionesTerm:
    """SEMAFORO_EXCEPCIONES_TERM_DAYS: 0 = unknown (both readings), 4 or 8 pins."""

    @staticmethod
    def _pin(monkeypatch, value) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "SEMAFORO_EXCEPCIONES_TERM_DAYS", value)

    def test_the_default_is_the_8_day_term_the_firm_works_with(self) -> None:
        """Shipping default = 8, so the panel shows ONE confirmed due date.

        The old default (0, "keep both readings") printed a "Plazo no
        confirmado: 4 días / 8 días" box on every excepciones plazo. The firm
        asked for the 4-day reading to be dropped and only the 8-day one kept,
        and that has to hold wherever the code runs -- setting it as an env var
        on the scraping station fixed the worker and left the API, which is
        what actually serves the panel, still on 0.
        """
        from app.config import Settings

        assert Settings().SEMAFORO_EXCEPCIONES_TERM_DAYS == 8

    def test_zero_still_restores_both_readings(self, monkeypatch) -> None:
        """The escape hatch stays: 0 is still a valid, env-settable value."""
        self._pin(monkeypatch, 0)
        term = confirmed_excepciones_term(object())
        assert term is None
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[2], term)
        assert a.confirmed is False and [r.term_days for r in a.readings] == [4, 8]

    def test_8_gives_one_confirmed_8_day_reading(self, monkeypatch) -> None:
        self._pin(monkeypatch, 8)
        term = confirmed_excepciones_term(object())
        assert term == 8
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[2], term)
        assert a.confirmed is True
        assert [r.term_days for r in a.readings] == [8]
        assert a.slot == "8d"

    def test_4_gives_one_confirmed_4_day_reading(self, monkeypatch) -> None:
        self._pin(monkeypatch, 4)
        term = confirmed_excepciones_term(object())
        assert term == 4
        a = assess("excepciones_8d", TRIGGER, DAY_DATE[2], term)
        assert a.confirmed is True
        assert [r.term_days for r in a.readings] == [4]
        assert a.slot == "4d"

    @pytest.mark.parametrize("bad", [1, 5, 7, 9, -4])
    def test_any_other_value_is_rejected_at_config_load(self, bad) -> None:
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError, match="SEMAFORO_EXCEPCIONES_TERM_DAYS"):
            Settings(SEMAFORO_EXCEPCIONES_TERM_DAYS=bad)

    @pytest.mark.parametrize("ok", [0, 4, 8])
    def test_valid_values_load(self, ok) -> None:
        from app.config import Settings

        assert Settings(SEMAFORO_EXCEPCIONES_TERM_DAYS=ok).SEMAFORO_EXCEPCIONES_TERM_DAYS == ok
