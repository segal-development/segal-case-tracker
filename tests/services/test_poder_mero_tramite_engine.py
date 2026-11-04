"""Engine behaviour of the extended acreditar_poder_3d trigger (Mero trámite /
Apercibimiento poder y/o título), scenarios of Rodrigo Campos.

The cutoff is pinned per test so the dates stay deterministic.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.config import settings
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine
from app.services.sync_service import poder_obligation_is_ours

PODER = "acreditar_poder_3d"
MERO = "Mero trámite"
APERC = "Apercibimiento poder y/o título"
TRASLADO = "Opone excepciones-Traslado"


@pytest.fixture(autouse=True)
def _cutoff(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "2026-09-22")


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut="22222222-2", email="m@test.com", name="Mero Lawyer")
    court = Court(name="Tribunal Civil", code="TCM01", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-MERO-1",
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


def _rows(db, case: Case) -> list[CaseDeadline]:
    return (
        db.query(CaseDeadline)
        .filter(CaseDeadline.case_id == case.id, CaseDeadline.deadline_type == PODER)
        .order_by(CaseDeadline.id)
        .all()
    )


def _opone(db, case: Case, when: datetime = datetime(2026, 9, 21)) -> Movement:
    return _add(db, case, when, "Opone excepciones", procedure="Escrito")


class TestScenarios:
    def test_1_compliance_two_days_later_is_cumplido(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), MERO)  # Monday
        _add(db, case, datetime(2026, 10, 7), "Acredita Poder", procedure="Escrito")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        assert row.status == "cumplido"
        assert row.triggered_at == date(2026, 10, 5)
        assert row.due_date == date(2026, 10, 8)
        assert row.verdict == "cumplido"

    def test_1b_cumple_lo_ordenado_is_cumplido_with_verdict(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), APERC)
        _add(db, case, datetime(2026, 10, 7), "Cumple lo ordenado", procedure="Escrito")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        assert (row.status, row.verdict) == ("cumplido", "cumplido")

    def test_2_no_compliance_past_due_is_expired_and_a_miss(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), MERO)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        assert row.status == "expired"
        assert row.verdict == "no_cumplido"
        assert row.due_date == date(2026, 10, 8)

    def test_3_traslado_without_resolution_creates_no_plazo(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), TRASLADO)
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_4_before_cutoff_no_plazo_but_previo_still_works(self, db, case) -> None:
        _opone(db, case, datetime(2026, 9, 1))
        _add(db, case, datetime(2026, 9, 10), MERO)
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []
        _add(db, case, datetime(2026, 9, 11), "Previo a proveer")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        assert row.triggered_at == date(2026, 9, 11)

    def test_5_mero_tramite_after_traslado_creates_no_plazo(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 1), TRASLADO)
        _add(db, case, datetime(2026, 10, 20), MERO, stage="Impugnación de Sentencia")
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_6_apercibimiento_before_any_opone_excepciones_creates_no_plazo(self, db, case) -> None:
        _add(db, case, datetime(2026, 10, 5), APERC, stage="Inicio de la Tramitación")
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_7_extension_off_behaves_as_today(self, db, case, monkeypatch) -> None:
        monkeypatch.setattr(settings, "PODER_EXTENDED_FROM", "")
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), MERO)
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_8_nulo_movement_is_ignored(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), f"[Nulo] {MERO}")
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_9_court_moving_to_traslado_after_the_mero_tramite_is_not_a_miss(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), MERO)
        DeadlineEngine.recompute_case(db, case)
        assert [r.status for r in _rows(db, case)] == ["expired"]
        _add(db, case, datetime(2026, 10, 9), TRASLADO)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        assert row.status == "superseded"
        assert row.verdict is None

    def test_9b_traslado_in_the_same_sync_creates_no_row_at_all(self, db, case) -> None:
        _opone(db, case)
        _add(db, case, datetime(2026, 10, 5), MERO)
        _add(db, case, datetime(2026, 10, 9), TRASLADO)
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case) == []

    def test_10_plaintiff_side_is_not_our_obligation(self, db, case) -> None:
        _opone(db, case)
        trigger = _add(db, case, datetime(2026, 10, 5), MERO)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case)
        row.status = "active"
        row.due_date = date(2099, 1, 1)  # alive, so only the side decides
        row.source_movement_id = trigger.id
        db.flush()
        assert poder_obligation_is_ours(db, case, row, date(2026, 10, 6)) is not None
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DTE", rut="22222222-2",
            persona_type="NATURAL", nombre="Mero Lawyer", natural_key=f"k{case.id}AB.DTE",
        ))
        db.flush()
        assert poder_obligation_is_ours(db, case, row, date(2026, 10, 6)) is None
