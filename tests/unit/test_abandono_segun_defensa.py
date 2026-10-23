"""Abandono (arts. 152/153 CPC) depends on whether the ejecutado presented a defense.

Carla Segal, 2026-10-23: the 6-month abandono "procede cuando hay excepciones opuestas y posterior a
eso no hay gestiones útiles en el procedimiento y no hay sentencia aún respecto de esta defensa".
"Si ya se dictó sentencia respecto a las excepciones o no se presentó defensa el plazo para solicitar
abandono se amplía a 3 años desde la última gestión útil del ejecutante."

NOTIFICADO is the state of an ejecutado who was notified and has not opposed excepciones, so it follows
the 3-year rule. MANDAMIENTO (not notified yet) keeps the general 6 months (art. 152): Carla did not cover
it. "Última gestión útil" is approximated by the last movement of any kind (known limitation).
"""

from datetime import date

import pytest

from app.services.deadline_engine import _compute_abandono

LAST = date(2026, 1, 15)
SEVEN_MONTHS = date(2026, 8, 20)      # > 6 months, < 3 years after LAST
FIVE_MONTHS = date(2026, 6, 10)       # < 6 months
THREE_YEARS = date(2029, 1, 16)       # > 3 years after LAST
JUST_UNDER_3Y = date(2029, 1, 14)


def _state(name):
    from app.services import deadline_engine as de
    return getattr(de.ProceduralState, name)


# --- 6 months: excepciones opuestas, todavía sin sentencia sobre ellas ---------------------------
@pytest.mark.parametrize(
    "state", ["EXCEPCIONES", "TRASLADO_EJECUTANTE", "ADMISIBILIDAD", "AUTO_PRUEBA", "CITACION_SENTENCIA"]
)
class TestSeisMesesConDefensaSinSentencia:
    def test_available_after_six_months(self, state):
        assert _compute_abandono(_state(state), LAST, SEVEN_MONTHS) is True

    def test_not_available_before_six_months(self, state):
        assert _compute_abandono(_state(state), LAST, FIVE_MONTHS) is False


# --- 3 años: sentencia dictada, o no se presentó defensa -----------------------------------------
@pytest.mark.parametrize("state", ["SENTENCIA", "REBELDE", "NOTIFICADO"])
class TestTresAniosConSentenciaOSinDefensa:
    def test_not_available_after_six_months(self, state):
        assert _compute_abandono(_state(state), LAST, SEVEN_MONTHS) is False

    def test_not_available_just_under_three_years(self, state):
        assert _compute_abandono(_state(state), LAST, JUST_UNDER_3Y) is False

    def test_available_after_three_years(self, state):
        assert _compute_abandono(_state(state), LAST, THREE_YEARS) is True


# --- no notificado todavía: regla general de 6 meses (art. 152) ----------------------------------
class TestMandamientoKeepsTheGeneralRule:
    def test_available_after_six_months(self):
        assert _compute_abandono(_state("MANDAMIENTO"), LAST, SEVEN_MONTHS) is True

    def test_not_available_before_six_months(self):
        assert _compute_abandono(_state("MANDAMIENTO"), LAST, FIVE_MONTHS) is False


# --- sin cambios ---------------------------------------------------------------------------------
class TestUnchanged:
    def test_terminada_and_indeterminate_never(self):
        assert _compute_abandono(_state("TERMINADA"), LAST, THREE_YEARS) is False
        assert _compute_abandono(_state("INDETERMINATE"), LAST, THREE_YEARS) is False

    def test_no_state_or_no_movement(self):
        assert _compute_abandono(None, LAST, THREE_YEARS) is False
        assert _compute_abandono(_state("EXCEPCIONES"), None, THREE_YEARS) is False
