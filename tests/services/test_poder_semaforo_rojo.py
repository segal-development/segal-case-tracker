"""The open "acreditar poder" obligation turns the case ROJO (forward-only).

Scenarios of odd/tasks/poder-semaforo-rojo.md (S6). The cutoff and the engine's
"today" are pinned per test so the dates stay deterministic.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.config import settings
from app.core.deadlines_config import ProceduralState
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services import deadline_engine
from app.services.deadline_engine import DeadlineEngine

PODER = "acreditar_poder_3d"
MERO = "Mero trámite"
TRASLADO = "Opone excepciones-Traslado"
MERO_DAY = datetime(2026, 10, 5)  # Monday; the 3 business days end on Thursday 8


@pytest.fixture(autouse=True)
def _pinned(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "2026-09-22")
    _set_today(monkeypatch, date(2026, 10, 5))


def _set_today(monkeypatch, day: date) -> None:
    monkeypatch.setattr(deadline_engine, "_today_chile", lambda: day)


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut="33333333-3", email="r@test.com", name="Rojo Lawyer")
    court = Court(name="Tribunal Civil", code="TCR01", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-ROJO-1",
        competencia="civil", filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _add(
    db, case: Case, when: datetime, description: str,
    stage: str = "Excepciones", procedure: str = "Resolución",
) -> Movement:
    mv = Movement(
        case_id=case.id, stage=stage, description=description,
        procedure=procedure, movement_date=when,
    )
    db.add(mv)
    db.flush()
    return mv


def _opone(db, case: Case) -> Movement:
    return _add(db, case, datetime(2026, 9, 21), "Opone excepciones", procedure="Escrito")


def _semaforo(db, case: Case) -> str:
    DeadlineEngine.recompute_case(db, case)
    return case.semaforo


def _rows(db, case: Case) -> list[CaseDeadline]:
    return (
        db.query(CaseDeadline)
        .filter(CaseDeadline.case_id == case.id, CaseDeadline.deadline_type == PODER)
        .order_by(CaseDeadline.id)
        .all()
    )


class TestNewObligationIsRojo:
    def test_1_dictated_today_unfulfilled_is_rojo_the_same_day(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, MERO)
        assert _semaforo(db, case) == "rojo"
        assert [r.status for r in _rows(db, case)] == ["active"]

    def test_2_complied_is_not_rojo(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, MERO)
        _add(db, case, datetime(2026, 10, 6), "Acredita Poder", procedure="Escrito")
        assert _semaforo(db, case) != "rojo"

    def test_3_court_moved_on_to_traslado_is_not_rojo(self, db, case, monkeypatch) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, MERO)
        assert _semaforo(db, case) == "rojo"
        _add(db, case, datetime(2026, 10, 6), TRASLADO)
        assert _semaforo(db, case) != "rojo"

    def test_4_past_the_three_days_unfulfilled_is_still_rojo(self, db, case, monkeypatch) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, MERO)
        _set_today(monkeypatch, date(2026, 10, 20))
        assert _semaforo(db, case) == "rojo"
        assert [r.status for r in _rows(db, case)] == ["expired"]

    def test_previo_a_proveer_on_or_after_the_cutoff_is_rojo(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, "Previo a proveer")
        assert _semaforo(db, case) == "rojo"


class TestUnchanged:
    def test_5_row_before_the_cutoff_keeps_todays_colour(self, db, case, monkeypatch) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, "Previo a proveer")
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "2026-10-06")
        colour = _semaforo(db, case)
        assert colour != "rojo"
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "")
        assert _semaforo(db, case) == colour

    def test_6_firm_demandante_is_not_rojo_because_of_it(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, "Previo a proveer")
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DTE", rut="33333333-3",
            persona_type="NATURAL", nombre="Rojo Lawyer", natural_key=f"k{case.id}AB.DTE",
        ))
        db.flush()
        assert _semaforo(db, case) != "rojo"

    def test_7_feature_off_keeps_todays_colour(self, db, case, monkeypatch) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, "Previo a proveer")
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "")
        assert _semaforo(db, case) != "rojo"

    def test_invalid_cutoff_is_off(self, db, case, monkeypatch) -> None:
        _opone(db, case)
        _add(db, case, MERO_DAY, "Previo a proveer")
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "not-a-date")
        assert _semaforo(db, case) != "rojo"


class TestDirectCompute:
    """`_compute_semaforo` with a live row, to pin the gris and row filters."""

    def _live(self, db, case):
        _opone(db, case)
        _add(db, case, MERO_DAY, MERO)
        DeadlineEngine.recompute_case(db, case)
        movements = db.query(Movement).filter(Movement.case_id == case.id).all()
        (row,) = _rows(db, case)
        return movements, row

    def _compute(self, db, case, movements, state=ProceduralState.EXCEPCIONES):
        return DeadlineEngine._compute_semaforo(
            db, case.id, state, date(2026, 10, 5),
            side="demandado", movements=movements,
        )

    def test_sanity_live_row_is_rojo(self, db, case) -> None:
        movements, _ = self._live(db, case)
        assert self._compute(db, case, movements) == "rojo"

    @pytest.mark.parametrize(
        "state", [ProceduralState.TERMINADA, ProceduralState.INDETERMINATE]
    )
    def test_8_terminada_and_indeterminate_are_gris(self, db, case, state) -> None:
        movements, _ = self._live(db, case)
        assert self._compute(db, case, movements, state) == "gris"

    @pytest.mark.parametrize("status", ["cumplido", "superseded"])
    def test_9_closed_row_never_colours(self, db, case, status) -> None:
        movements, row = self._live(db, case)
        row.status = status
        db.flush()
        assert self._compute(db, case, movements) != "rojo"

    def test_9b_manual_row_never_colours(self, db, case) -> None:
        movements, row = self._live(db, case)
        row.is_manual = True
        db.flush()
        assert self._compute(db, case, movements) != "rojo"

    def test_default_call_without_side_is_unchanged(self, db, case) -> None:
        self._live(db, case)
        colour = DeadlineEngine._compute_semaforo(
            db, case.id, ProceduralState.EXCEPCIONES, date(2026, 10, 5)
        )
        assert colour != "rojo"
