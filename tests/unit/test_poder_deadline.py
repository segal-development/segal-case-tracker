"""Pure detection of the "acreditar patrocinio y poder" obligation.

Real data (QA): ``Apercibimiento poder y/o título`` is a COURT resolution
(7,078 movements / 6,336 cases, none carrying ``Diligencia:``), so the plazo
anchors to ``movement_date``. Annulled movements carry a ``[Nulo]`` prefix and
must never trigger anything.
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

APERCIBIMIENTO = "Apercibimiento poder y/o título"


def _mv(day: str, description: str, stage: str = "Inicio de la Tramitación") -> FakeMovement:
    return FakeMovement(
        stage=stage,
        procedure="Resolución",
        description=description,
        movement_date=datetime.strptime(day, "%Y-%m-%d"),
    )


class TestDeadlineTypeConfig:
    def test_three_business_days_not_fatal_and_cited_without_article(self) -> None:
        dt = DeadlineType.ACREDITAR_PODER_3D
        assert dt.value == "acreditar_poder_3d"
        assert dt.dias_habiles == 3
        # Pending Dirección Jurídica: never claim fatality / an article we have not verified.
        assert dt.is_fatal is False
        assert dt.legal_basis == "Ley 18.120"

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
    def test_apercibimiento_is_detected_and_unfulfilled(self) -> None:
        mv = _mv("2026-06-01", APERCIBIMIENTO)
        result = find_poder_obligation([mv])
        assert result is not None
        assert result.trigger is mv
        assert result.fulfilled is False

    def test_no_apercibimiento_returns_none(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", "Acredita Poder")]) is None
        assert find_poder_obligation([]) is None

    def test_nulo_apercibimiento_does_not_trigger(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", f"[Nulo] {APERCIBIMIENTO}")]) is None

    def test_nulo_prefix_is_case_insensitive_and_tolerates_leading_space(self) -> None:
        assert find_poder_obligation([_mv("2026-06-01", f"  [NULO] {APERCIBIMIENTO}")]) is None

    def test_valid_apercibimiento_survives_a_nulo_one_after_it(self) -> None:
        valid = _mv("2026-06-01", APERCIBIMIENTO)
        result = find_poder_obligation([valid, _mv("2026-06-05", f"[Nulo] {APERCIBIMIENTO}")])
        assert result is not None and result.trigger is valid

    def test_latest_valid_apercibimiento_wins(self) -> None:
        first = _mv("2026-05-01", APERCIBIMIENTO)
        second = _mv("2026-06-01", APERCIBIMIENTO)
        result = find_poder_obligation([second, first])  # order-independent
        assert result is not None and result.trigger is second

    @pytest.mark.parametrize(
        "desc",
        ["Acredita Poder", "Poder acreditado/acompaña patrocinio", "Patrocinio y poder", "Delega poder"],
    )
    def test_later_compliance_marks_fulfilled(self, desc: str) -> None:
        result = find_poder_obligation(
            [_mv("2026-06-01", APERCIBIMIENTO), _mv("2026-06-04", desc)]
        )
        assert result is not None and result.fulfilled is True

    def test_earlier_compliance_does_not_fulfil(self) -> None:
        result = find_poder_obligation(
            [_mv("2026-05-20", "Acredita Poder"), _mv("2026-06-01", APERCIBIMIENTO)]
        )
        assert result is not None and result.fulfilled is False

    def test_same_day_compliance_does_not_fulfil(self) -> None:
        """Strictly posterior: a same-day movement cannot be told apart from the demand's own filings."""
        result = find_poder_obligation(
            [_mv("2026-06-01", APERCIBIMIENTO), _mv("2026-06-01", "Acredita Poder")]
        )
        assert result is not None and result.fulfilled is False

    def test_nulo_compliance_does_not_fulfil(self) -> None:
        result = find_poder_obligation(
            [_mv("2026-06-01", APERCIBIMIENTO), _mv("2026-06-04", "[Nulo] Acredita Poder")]
        )
        assert result is not None and result.fulfilled is False

    def test_accepts_plain_date_movement_dates(self) -> None:
        mv = FakeMovement(
            stage="x", procedure="y", description=APERCIBIMIENTO, movement_date=date(2026, 6, 1)  # type: ignore[arg-type]
        )
        assert find_poder_obligation([mv]) is not None
