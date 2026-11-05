"""What the lawyer reads. Neutral Spanish, tuteo, and above all: an alert never
asserts a deadline we do not know."""

import re
from datetime import date

import pytest

from app.core.semaforo_config import Level
from app.services.deadline_semaforo import assess
from app.services.plazo_copy import build_alert_text

TRIGGER = date(2026, 11, 2)
DAY = {
    1: date(2026, 11, 3), 2: date(2026, 11, 4), 3: date(2026, 11, 5),
    4: date(2026, 11, 6), 6: date(2026, 11, 9), 8: date(2026, 11, 11),
}
ROL, TRIBUNAL = "C-123-2026", "1º Juzgado Civil de Santiago"
VOSEO = re.compile(r"\b(vos|tenés|podés|sabés|confirmá|revisá|mirá|fijate|sos)\b", re.I)


def _text(deadline_type, day, confirmed_term=None):
    a = assess(deadline_type, TRIGGER, DAY[day], confirmed_term)
    title, message = build_alert_text(a, ROL, TRIBUNAL)
    return a, title, message


class TestUnconfirmedExcepciones:
    def test_title_and_message_declare_it_is_unconfirmed(self) -> None:
        _, title, message = _text("excepciones_8d", 3)
        assert "sin confirmar" in title.lower()
        assert "SIN CONFIRMAR" in message

    def test_both_candidate_dates_appear_and_neither_is_asserted(self) -> None:
        _, _, message = _text("excepciones_8d", 3)
        assert "06-11-2026" in message  # 4-day reading
        assert "11-11-2026" in message  # 8-day reading
        assert "comuna asiento del tribunal" in message
        assert "fuera de la comuna asiento del tribunal" in message

    def test_the_title_hedges_the_date(self) -> None:
        _, title, _ = _text("excepciones_8d", 3)
        assert "podría vencer el 06-11-2026" in title

    def test_never_uses_the_confirmed_phrasing(self) -> None:
        """'Vence el' / 'Plazo de N días hábiles:' is the voice of a known term."""
        for day in (1, 3, 4, 6, 8):
            _, title, message = _text("excepciones_8d", day)
            assert "Vence el" not in message
            assert "Último día del plazo" not in message
            assert "Plazo para oponer excepciones: 8" not in message
            assert "Plazo para oponer excepciones: 4" not in message

    def test_says_where_the_fact_lives_and_what_to_do_meanwhile(self) -> None:
        _, _, message = _text("excepciones_8d", 3)
        assert "estampado del receptor" in message
        assert "fecha más cercana" in message

    def test_a_reading_that_already_lapsed_is_said_in_the_past(self) -> None:
        _, _, message = _text("excepciones_8d", 6)
        assert "venció el 06-11-2026" in message
        assert "vence el 11-11-2026" in message
        # and the earliest LIVE date is the one to work against
        assert "límite la fecha más cercana aún vigente: 11-11-2026" in message

    def test_day_is_counted_from_the_trigger(self) -> None:
        _, _, message = _text("excepciones_8d", 4)
        assert "Día 4" in message and "02-11-2026" in message


class TestConfirmedExcepciones:
    def test_confirmed_8_states_the_term(self) -> None:
        _, title, message = _text("excepciones_8d", 6, confirmed_term=8)
        assert "sin confirmar" not in title.lower()
        assert "SIN CONFIRMAR" not in message
        assert "8 días hábiles" in message
        assert "Vence el 11-11-2026" in message
        assert "Día 6 de 8" in message

    def test_confirmed_4_states_the_term(self) -> None:
        _, _, message = _text("excepciones_8d", 3, confirmed_term=4)
        assert "4 días hábiles" in message
        assert "Vence el 06-11-2026" in message
        assert "11-11-2026" not in message

    def test_last_day_is_named(self) -> None:
        _, _, message = _text("excepciones_8d", 8, confirmed_term=8)
        assert "Último día del plazo: 11-11-2026" in message


class TestRatificacion:
    def test_three_day_term_with_the_consequence(self) -> None:
        _, title, message = _text("acreditar_poder_3d", 2)
        assert "Ratificación de firma" in title and ROL in title
        assert "3 días hábiles" in message
        assert "Vence el 05-11-2026" in message
        assert "tener por no presentado el escrito" in message
        assert "SIN CONFIRMAR" not in message


class TestEveryAlert:
    @pytest.mark.parametrize("deadline_type", ["excepciones_8d", "acreditar_poder_3d"])
    @pytest.mark.parametrize("day", [1, 2, 3])
    def test_carries_level_tag_tribunal_disclaimer_and_no_voseo(
        self, deadline_type, day
    ) -> None:
        a, title, message = _text(deadline_type, day)
        assert title.startswith(a.level.value.upper() + " · ")
        assert TRIBUNAL in message
        assert "no reemplaza el criterio del abogado" in message
        assert not VOSEO.search(title + " " + message)

    def test_a_lapsed_assessment_has_no_copy(self) -> None:
        a = assess("excepciones_8d", TRIGGER, date(2026, 11, 20))
        assert a.level is None
        with pytest.raises(ValueError):
            build_alert_text(a, ROL, TRIBUNAL)
