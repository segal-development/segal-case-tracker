"""Extended trigger of the acreditar_poder_3d plazo: ``Mero trámite`` and
``Apercibimiento poder y/o título`` dictated after our ``Opone excepciones`` and
before the court moves on to ``Opone excepciones-Traslado``.

Forward-only: it only applies from the ``PODER_EXTENDED_FROM`` cutoff on.
``Previo a proveer`` keeps its behaviour for the whole history.
"""

from __future__ import annotations

from datetime import date, datetime
import logging

import pytest

from app.config import settings
from app.services.poder_deadline import find_poder_obligation, parse_extended_from
from tests.fixtures.real_case_movements import FakeMovement

CUTOFF = date(2026, 11, 4)
MERO = "Mero trámite"
APERC = "Apercibimiento poder y/o título"
TRASLADO = "Opone excepciones-Traslado"


def _mv(
    day: str,
    description: str,
    procedure: str = "Resolución",
    id: int = 0,
) -> FakeMovement:
    return FakeMovement(
        stage="Excepciones",
        procedure=procedure,
        description=description,
        movement_date=datetime.strptime(day, "%Y-%m-%d"),
        id=id,
    )


def _opone(day: str = "2026-10-20") -> FakeMovement:
    return _mv(day, "Opone excepciones", procedure="Escrito", id=1)


class TestExtendedTrigger:
    @pytest.mark.parametrize("text", [MERO, "Mero tramite", "MERO TRÁMITE", APERC])
    def test_mero_tramite_and_apercibimiento_after_cutoff_trigger(self, text) -> None:
        trigger = _mv("2026-11-05", text, id=2)
        result = find_poder_obligation([_opone(), trigger], extended_from=CUTOFF)
        assert result is not None
        assert result.trigger is trigger
        assert result.fulfilled is False

    def test_cutoff_day_itself_triggers(self) -> None:
        result = find_poder_obligation(
            [_opone(), _mv("2026-11-04", MERO, id=2)], extended_from=CUTOFF
        )
        assert result is not None

    def test_before_cutoff_does_not_trigger(self) -> None:
        assert find_poder_obligation(
            [_opone(), _mv("2026-11-03", MERO, id=2)], extended_from=CUTOFF
        ) is None

    def test_previo_a_proveer_before_cutoff_still_triggers(self) -> None:
        result = find_poder_obligation(
            [_mv("2026-06-01", "Previo a proveer")], extended_from=CUTOFF
        )
        assert result is not None

    def test_requires_a_prior_opone_excepciones_escrito(self) -> None:
        assert find_poder_obligation(
            [_mv("2026-11-05", APERC, id=2)], extended_from=CUTOFF
        ) is None
        # Same day does not count as "before": the escrito precedes by (date, id).
        assert find_poder_obligation(
            [_mv("2026-11-05", MERO, id=1), _opone("2026-11-05")], extended_from=CUTOFF
        ) is None

    def test_opone_excepciones_resolution_is_not_a_filing(self) -> None:
        resolution = _mv("2026-10-20", "Opone excepciones", procedure="Resolución", id=1)
        assert find_poder_obligation(
            [resolution, _mv("2026-11-05", MERO, id=2)], extended_from=CUTOFF
        ) is None

    def test_no_trigger_when_traslado_already_dictated(self) -> None:
        movements = [_opone(), _mv("2026-11-05", TRASLADO, id=2), _mv("2026-11-10", MERO, id=3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF) is None

    def test_same_day_traslado_means_court_already_moved_on(self) -> None:
        movements = [_opone(), _mv("2026-11-05", TRASLADO, id=2), _mv("2026-11-05", MERO, id=3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF) is None

    def test_nulo_traslado_does_not_count_as_moved_on(self) -> None:
        movements = [_opone(), _mv("2026-11-04", f"[Nulo] {TRASLADO}", id=2), _mv("2026-11-05", MERO, id=3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF) is not None

    def test_nulo_and_escrito_are_ignored(self) -> None:
        assert find_poder_obligation(
            [_opone(), _mv("2026-11-05", f"[Nulo] {MERO}", id=2)], extended_from=CUTOFF
        ) is None
        assert find_poder_obligation(
            [_opone(), _mv("2026-11-05", MERO, procedure="Escrito", id=2)], extended_from=CUTOFF
        ) is None

    def test_extension_off_behaves_as_before(self) -> None:
        assert find_poder_obligation(
            [_opone(), _mv("2026-11-05", MERO, id=2)], extended_from=None
        ) is None

    def test_latest_trigger_wins(self) -> None:
        later = _mv("2026-11-09", MERO, id=3)
        previo = _mv("2026-11-05", "Previo a proveer", id=2)
        result = find_poder_obligation([_opone(), previo, later], extended_from=CUTOFF)
        assert result.trigger is later

    def test_description_must_start_with_the_wording(self) -> None:
        assert find_poder_obligation(
            [_opone(), _mv("2026-11-05", "Resuelve mero trámite", id=2)], extended_from=CUTOFF
        ) is None


class TestCompliance:
    def test_acredita_poder_after_trigger_fulfils(self) -> None:
        movements = [_opone(), _mv("2026-11-05", MERO, id=2), _mv("2026-11-07", "Acredita Poder", "Escrito", 3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF).fulfilled is True

    def test_cumple_lo_ordenado_fulfils_new_triggers(self) -> None:
        movements = [_opone(), _mv("2026-11-05", MERO, id=2), _mv("2026-11-07", "Cumple lo ordenado", "Escrito", 3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF).fulfilled is True

    def test_cumple_lo_ordenado_does_not_fulfil_previo_a_proveer(self) -> None:
        movements = [_mv("2026-06-01", "Previo a proveer"), _mv("2026-06-02", "Cumple lo ordenado", "Escrito", 3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF).fulfilled is False

    def test_same_day_compliance_does_not_fulfil(self) -> None:
        movements = [_opone(), _mv("2026-11-05", MERO, id=2), _mv("2026-11-05", "Cumple lo ordenado", "Escrito", 3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF).fulfilled is False


class TestCourtMovedOn:
    def test_traslado_after_unfulfilled_new_trigger_is_not_an_obligation(self) -> None:
        movements = [_opone(), _mv("2026-11-05", MERO, id=2), _mv("2026-11-09", TRASLADO, id=3)]
        assert find_poder_obligation(movements, extended_from=CUTOFF) is None

    def test_traslado_after_fulfilled_new_trigger_stays_fulfilled(self) -> None:
        movements = [
            _opone(), _mv("2026-11-05", MERO, id=2),
            _mv("2026-11-06", "Acredita Poder", "Escrito", 3), _mv("2026-11-09", TRASLADO, id=4),
        ]
        assert find_poder_obligation(movements, extended_from=CUTOFF).fulfilled is True

    def test_previo_a_proveer_ignores_a_later_traslado(self) -> None:
        movements = [_opone(), _mv("2026-11-05", "Previo a proveer", id=2), _mv("2026-11-09", TRASLADO, id=3)]
        result = find_poder_obligation(movements, extended_from=CUTOFF)
        assert result is not None and result.fulfilled is False


class TestCutoffSetting:
    def test_parse_valid_empty_and_invalid(self, caplog) -> None:
        assert parse_extended_from("2026-11-04") == date(2026, 11, 4)
        assert parse_extended_from("") is None
        assert parse_extended_from("  ") is None
        with caplog.at_level(logging.WARNING):
            assert parse_extended_from("not-a-date") is None
        assert "PODER_EXTENDED_FROM" in caplog.text

    def test_default_setting_is_today_of_the_decision(self) -> None:
        assert type(settings).model_fields["PODER_EXTENDED_FROM"].default == "2026-11-04"

    def test_default_cutoff_comes_from_settings(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "2026-11-04")
        assert find_poder_obligation([_opone(), _mv("2026-11-05", MERO, id=2)]) is not None
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "")
        assert find_poder_obligation([_opone(), _mv("2026-11-05", MERO, id=2)]) is None
