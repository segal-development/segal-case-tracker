"""Pure detection of the "ratificar firma / acreditar patrocinio y poder" obligation.

The trigger is the court's ``Previo a proveer`` resolution (2,174 cases in QA),
NOT ``Apercibimiento poder y/o título`` (a different movement: 6,339 cases, only
1,919 overlap). It must be issued by the tribunal: ``Movement.procedure`` is
PJUD's "Trámite" column, ``Resolución`` or ``Escrito``; an ``Escrito`` is a
party's filing and can never be this order. Annulled movements carry a
``[Nulo]`` prefix and must never trigger anything.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.deadlines_config import (
    ACTIONABLE_DEADLINE_VALUES,
    DEADLINE_LABELS,
    INFORMATIONAL_DEADLINES,
    DeadlineType,
    actionable_sets,
)
from app.services.poder_deadline import find_poder_obligation
from tests.fixtures.real_case_movements import FakeMovement

PREVIO = "Previo a proveer"


def _mv(
    day: str,
    description: str,
    stage: str = "Inicio de la Tramitación",
    procedure: str = "Resolución",
) -> FakeMovement:
    return FakeMovement(
        stage=stage,
        procedure=procedure,
        description=description,
        movement_date=datetime.strptime(day, "%Y-%m-%d"),
    )


class TestDeadlineTypeConfig:
    def test_three_business_days_not_fatal_and_cited_without_article(self) -> None:
        dt = DeadlineType.ACREDITAR_PODER_3D
        assert dt.value == "acreditar_poder_3d"
        assert dt.dias_habiles == 3
        # Pending Dirección Jurídica: never claim fatality without a lawyer confirming it.
        assert dt.is_fatal is False
        assert dt.legal_basis == "art. 7 CPC · art. 7 Ley 20.886 (mod. Ley 21.394)"

    def test_has_a_display_label(self) -> None:
        assert DEADLINE_LABELS["acreditar_poder_3d"]

    def test_is_informational_never_actionable_for_either_side(self) -> None:
        """Not actionable => never drives the semáforo, next_deadline_at or alerts."""
        assert DeadlineType.ACREDITAR_PODER_3D in INFORMATIONAL_DEADLINES
        assert "acreditar_poder_3d" not in ACTIONABLE_DEADLINE_VALUES
        for side in ("demandante", "demandado"):
            mandatory, actionable = actionable_sets(side)
            assert "acreditar_poder_3d" not in mandatory
            assert "acreditar_poder_3d" not in actionable


class TestFindPoderObligation:
    def test_previo_a_proveer_is_detected_and_unfulfilled(self) -> None:
        mv = _mv("2026-06-01", PREVIO)
        result = find_poder_obligation([mv])
        assert result is not None
        assert result.trigger is mv
        assert result.fulfilled is False

    def test_no_previo_a_proveer_returns_none(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", "Acredita Poder")]) is None
        assert find_poder_obligation([]) is None

    def test_old_apercibimiento_poder_titulo_no_longer_triggers(self) -> None:
        """Different movement (only 1,919 of the cases overlap): never unify them."""
        assert find_poder_obligation([_mv("2026-06-01", "Apercibimiento poder y/o título")]) is None

    def test_escrito_previo_a_proveer_does_not_trigger(self) -> None:
        """An Escrito is a party's filing, never the tribunal's order."""
        assert find_poder_obligation([_mv("2026-06-01", PREVIO, procedure="Escrito")]) is None

    @pytest.mark.parametrize("procedure", ["", None, "Resolución"])
    def test_blank_or_resolucion_procedure_triggers(self, procedure) -> None:
        """Exclude Escrito rather than require Resolución: 56 real movements have it blank."""
        assert find_poder_obligation([_mv("2026-06-01", PREVIO, procedure=procedure)]) is not None

    def test_nulo_previo_a_proveer_does_not_trigger(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", f"[Nulo] {PREVIO}")]) is None

    def test_nulo_prefix_is_case_insensitive_and_tolerates_leading_space(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", f"  [NULO] {PREVIO}")]) is None

    def test_valid_previo_a_proveer_survives_a_nulo_one_after_it(self) -> None:
        valid = _mv("2026-06-01", PREVIO)
        result = find_poder_obligation([valid, _mv("2026-06-05", f"[Nulo] {PREVIO}")])
        assert result is not None and result.trigger is valid

    def test_latest_valid_previo_a_proveer_wins(self) -> None:
        first = _mv("2026-05-01", PREVIO)
        second = _mv("2026-06-01", PREVIO)
        result = find_poder_obligation([second, first])  # order-independent
        assert result is not None and result.trigger is second

    @pytest.mark.parametrize(
        "desc",
        ["Acredita Poder", "Poder acreditado/acompaña patrocinio", "Patrocinio y poder", "Delega poder"],
    )
    def test_later_compliance_marks_fulfilled(self, desc: str) -> None:
        result = find_poder_obligation(
            [_mv("2026-06-01", PREVIO), _mv("2026-06-04", desc)]
        )
        assert result is not None and result.fulfilled is True

    def test_earlier_compliance_does_not_fulfil(self) -> None:
        result = find_poder_obligation(
            [_mv("2026-05-20", "Acredita Poder"), _mv("2026-06-01", PREVIO)]
        )
        assert result is not None and result.fulfilled is False

    def test_same_day_compliance_does_not_fulfil(self) -> None:
        """Strictly posterior: a same-day movement cannot be told apart from the demand's own filings."""
        result = find_poder_obligation(
            [_mv("2026-06-01", PREVIO), _mv("2026-06-01", "Acredita Poder")]
        )
        assert result is not None and result.fulfilled is False

    def test_nulo_compliance_does_not_fulfil(self) -> None:
        result = find_poder_obligation(
            [_mv("2026-06-01", PREVIO), _mv("2026-06-04", "[Nulo] Acredita Poder")]
        )
        assert result is not None and result.fulfilled is False

    def test_accepts_plain_date_movement_dates(self) -> None:
        mv = FakeMovement(
            stage="x", procedure="y", description=PREVIO, movement_date=date(2026, 6, 1)  # type: ignore[arg-type]
        )
        assert find_poder_obligation([mv]) is not None
