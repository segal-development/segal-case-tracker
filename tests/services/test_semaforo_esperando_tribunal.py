"""Prueba stage: a lapsed prueba plazo with an idle court is "esperando al tribunal", not red.

Only the four prueba-stage types are affected, and only once past due. A prueba plazo still
running counts as always; every other plazo type, the liberación override and the poder rule are
unchanged. The deadline rows are never rewritten.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.deadlines_config import DeadlineType, ProceduralState, actionable_sets
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine

TODAY = date(2026, 11, 6)
RUT = "11111111-1"
PRUEBA = (
    DeadlineType.TERMINO_PROBATORIO_10D,
    DeadlineType.OBSERVACIONES_PRUEBA_6D,
    DeadlineType.LISTA_TESTIGOS_2D,
    DeadlineType.REPOSICION_AUTO_PRUEBA_3D,
)


@pytest.fixture
def case(db, monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
    lawyer = Lawyer(rut=RUT, email="t@t.cl", name="Test Lawyer", is_active=True)
    court = Court(name="Juzgado Civil", code="C001", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-5631-2022", competencia="civil",
        procedure="Ordinario", status="active", plaintiff="BANCO X", defendant="PEREZ",
        filed_at=datetime(2022, 1, 1),
    )
    db.add(c)
    db.flush()
    db.add_all([
        CaseLitigante(case_id=c.id, participante="AB.DDO", rut=RUT, persona_type="NATURAL",
                      nombre="Abogado", natural_key="ab1"),
        CaseLitigante(case_id=c.id, participante="DDO.", rut="18765432-1", persona_type="NATURAL",
                      nombre="JUAN PEREZ", natural_key="cl1"),
        Movement(case_id=c.id, stage="Prueba", procedure="",
                 movement_date=datetime(2024, 5, 20), description="Notificación del auto de prueba"),
    ])
    db.commit()
    return c


def _row(db, case, dtype, due, *, status="active"):
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=dtype.value, due_date=due,
        triggered_at=due - timedelta(days=10), status=status,
    ))
    db.commit()


def _state(db, case, state=ProceduralState.AUTO_PRUEBA):
    mandatory, actionable = actionable_sets("demandado")
    return DeadlineEngine._compute_semaforo_state(
        db, case.id, state, TODAY, mandatory_values=mandatory, actionable_values=actionable
    )


def _statuses(db, case):
    return sorted((d.deadline_type, d.status, d.due_date) for d in
                  db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id))


class TestExpiredPruebaIsNotRed:
    def test_expired_observaciones_c_5631_2022(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        assert _state(db, case) == ("verde", True)

    @pytest.mark.parametrize("dtype", [
        DeadlineType.TERMINO_PROBATORIO_10D,
        DeadlineType.OBSERVACIONES_PRUEBA_6D,
        DeadlineType.LISTA_TESTIGOS_2D,
    ])
    def test_each_mandatory_prueba_type_expired(self, db, case, dtype) -> None:
        _row(db, case, dtype, date(2024, 6, 14), status="expired")
        assert _state(db, case) == ("verde", True)

    def test_active_but_overdue_observaciones_c_1479_2026(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2026, 10, 29))
        assert _state(db, case) == ("verde", True)

    def test_reposicion_was_never_red_so_it_is_not_esperando(self, db, case) -> None:
        _row(db, case, DeadlineType.REPOSICION_AUTO_PRUEBA_3D, date(2024, 6, 14), status="expired")
        assert _state(db, case) == ("verde", False)

    def test_rows_are_not_rewritten(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.LISTA_TESTIGOS_2D, date(2026, 10, 29))
        before = _statuses(db, case)
        _state(db, case)
        db.expire_all()
        assert _statuses(db, case) == before


class TestPruebaStillRunningCountsAsBefore:
    def test_due_tomorrow_is_rojo(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, add_business_days(TODAY, 1))
        assert _state(db, case) == ("rojo", False)

    def test_due_today_is_rojo(self, db, case) -> None:
        _row(db, case, DeadlineType.LISTA_TESTIGOS_2D, TODAY)
        assert _state(db, case) == ("rojo", False)

    def test_four_business_days_is_amarillo(self, db, case) -> None:
        _row(db, case, DeadlineType.TERMINO_PROBATORIO_10D, add_business_days(TODAY, 4))
        assert _state(db, case) == ("amarillo", False)

    def test_running_prueba_wins_over_an_expired_one(self, db, case) -> None:
        _row(db, case, DeadlineType.LISTA_TESTIGOS_2D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, add_business_days(TODAY, 1))
        assert _state(db, case) == ("rojo", False)


class TestOtherRulesUnchanged:
    def test_expired_prueba_plus_overdue_fatal_plazo_stays_rojo(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.EXCEPCIONES_8D, date(2026, 10, 29))
        assert _state(db, case) == ("rojo", False)

    def test_expired_prueba_plus_expired_mandatory_other_stays_rojo(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.EXCEPCIONES_8D, date(2024, 6, 14), status="expired")
        assert _state(db, case) == ("rojo", False)

    def test_expired_prueba_plus_running_other_plazo_follows_that_plazo(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.EXCEPCIONES_8D, add_business_days(TODAY, 4))
        assert _state(db, case) == ("amarillo", False)

    def test_expired_prueba_plus_far_other_plazo_is_verde_not_esperando(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        _row(db, case, DeadlineType.EXCEPCIONES_8D, add_business_days(TODAY, 12))
        assert _state(db, case) == ("verde", False)

    def test_expired_non_prueba_mandatory_is_still_rojo(self, db, case) -> None:
        _row(db, case, DeadlineType.EXCEPCIONES_8D, date(2024, 6, 14), status="expired")
        assert _state(db, case) == ("rojo", False)

    def test_no_prueba_plazos_unchanged(self, db, case) -> None:
        assert _state(db, case) == ("verde", False)

    def test_compute_semaforo_still_returns_the_colour_only(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        assert DeadlineEngine._compute_semaforo(db, case.id, ProceduralState.AUTO_PRUEBA, TODAY) == "verde"

    def test_gris_state_is_never_esperando(self, db, case) -> None:
        _row(db, case, DeadlineType.OBSERVACIONES_PRUEBA_6D, date(2024, 6, 14), status="expired")
        assert _state(db, case, ProceduralState.TERMINADA) == ("gris", False)


def _prueba_movements(db, case, *, with_prueba=True):
    db.add(Movement(case_id=case.id, stage="Gestión", procedure="",
                    movement_date=datetime(2024, 1, 10), description="NOTIFICACIÓN DE DEMANDA (Exitosa)"))
    if with_prueba:
        db.add(Movement(case_id=case.id, stage="Prueba", procedure="",
                        movement_date=datetime(2024, 5, 20),
                        description="Notificación resolución que recibe la causa a prue (Exitosa)"))
    db.commit()


class TestPersistedByRecompute:
    """recompute_case persists the flag; a liberación override still wins the colour."""

    def test_recompute_persists_flag_and_override_wins(self, db, case) -> None:
        _prueba_movements(db, case)
        DeadlineEngine.recompute_case(db, case)
        assert case.esperando_tribunal is True
        assert case.semaforo != "rojo"

        case.semaforo_override = "amarillo"
        case.semaforo_override_at = datetime(2026, 11, 5, 10, 0)
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo == "amarillo"
        assert case.esperando_tribunal is False

    def test_recompute_without_prueba_plazos_is_false(self, db, case) -> None:
        _prueba_movements(db, case, with_prueba=False)
        DeadlineEngine.recompute_case(db, case)
        assert case.esperando_tribunal is False
