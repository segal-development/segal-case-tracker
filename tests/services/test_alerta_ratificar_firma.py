"""Alert for the FATAL "ratificar firma" plazo (ACREDITAR_PODER_3D) — only when
the obligation is provably OURS (heuristic, see ``poder_deadline``).

The plazo type stays informational: it must never move the semáforo or
``next_deadline_at``. The alert is a separate path, emitted once per obligation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.sync_service import NotifyBudget, _maybe_recompute_deadlines

PREVIO = "Previo a proveer"
ALERT_TYPE = "ratificar_firma"
FIRM_RUT = "22222222-2"


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut=FIRM_RUT, email="r@test.com", name="Ratifica Lawyer")
    court = Court(name="Tribunal Civil", code="TCR01", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-RATIF-1",
        competencia="civil", filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _add(db, case, when, description, procedure="Resolución") -> Movement:
    mv = Movement(
        case_id=case.id, stage="Inicio de la Tramitación", description=description,
        procedure=procedure, movement_date=when,
    )
    db.add(mv)
    db.flush()
    return mv


def _side(db, case, participante: str) -> None:
    db.add(CaseLitigante(
        case_id=case.id, participante=participante, rut=FIRM_RUT,
        persona_type="NATURAL", nombre="Ratifica Lawyer",
        natural_key=f"k{case.id}{participante}",
    ))
    db.flush()


def _run(db, case) -> None:
    with patch("app.services.sync_service.NotificationService"):
        _maybe_recompute_deadlines(db, case, budget=NotifyBudget(50))


def _alerts(db, case) -> list[Alert]:
    return db.query(Alert).filter(Alert.case_id == case.id, Alert.type == ALERT_TYPE).all()


def _excepciones_then_previo(db, case, *, excepciones="Opone excepciones",
                             procedure="Escrito") -> Movement:
    now = datetime.now()
    _add(db, case, now - timedelta(days=3), excepciones, procedure=procedure)
    return _add(db, case, now - timedelta(days=1), PREVIO)


class TestAlertsWhenObligationIsOurs:
    def test_previo_right_after_our_excepciones_alerts(self, db, case) -> None:
        _side(db, case, "AB.DDO")  # the firm defends the ejecutado
        previo = _excepciones_then_previo(db, case)

        _run(db, case)

        (alert,) = _alerts(db, case)
        assert alert.movement_id == previo.id
        assert alert.lawyer_id == case.lawyer_id
        assert "C-RATIF-1" in alert.title

    def test_previo_right_after_excepciones_does_not_alert_when_firm_is_demandante(
        self, db, case
    ) -> None:
        _side(db, case, "AB.DTE")  # the excepciones are the counterparty's
        _excepciones_then_previo(db, case)

        _run(db, case)

        assert _alerts(db, case) == []

    def test_previo_after_something_else_does_not_alert(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case, excepciones="Incidente")

        _run(db, case)

        assert _alerts(db, case) == []

    def test_excepciones_resolution_is_the_courts_ruling_not_our_filing(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case, procedure="Resolución")

        _run(db, case)

        assert _alerts(db, case) == []

    def test_annulled_excepciones_does_not_alert(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case, excepciones="[Nulo] Opone excepciones")

        _run(db, case)

        assert _alerts(db, case) == []

    def test_fulfilled_obligation_does_not_alert(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case)
        _add(db, case, datetime.now(), "Acredita Poder", procedure="Escrito")

        _run(db, case)

        assert _alerts(db, case) == []

    def test_expired_obligation_does_not_alert_on_backfill(self, db, case) -> None:
        """Months-old unfulfilled Previos must not fire on the first run after deploy."""
        _side(db, case, "AB.DDO")
        now = datetime.now()
        _add(db, case, now - timedelta(days=62), "Opone excepciones", procedure="Escrito")
        _add(db, case, now - timedelta(days=60), PREVIO)

        _run(db, case)

        assert _alerts(db, case) == []

    def test_another_escrito_between_our_excepciones_and_the_resolution_still_alerts(
        self, db, case
    ) -> None:
        """Formerly a pinned false negative (only the immediately previous movement
        counted). Measured on QA it missed 414 of 1,358 causas, so the rule was
        widened: if the in-between escrito is ours, the duty to ratify is ours
        anyway. A false positive costs an email; a false negative costs a defense."""
        _side(db, case, "AB.DDO")
        now = datetime.now()
        _add(db, case, now - timedelta(days=4), "Opone excepciones", procedure="Escrito")
        _add(db, case, now - timedelta(days=3), "Tenga presente", procedure="Escrito")
        _add(db, case, now - timedelta(days=1), PREVIO)

        _run(db, case)

        assert len(_alerts(db, case)) == 1

    def test_two_of_our_escritos_between_excepciones_and_resolution_alert(
        self, db, case
    ) -> None:
        _side(db, case, "AB.DDO")
        now = datetime.now()
        _add(db, case, now - timedelta(days=5), "Opone excepciones", procedure="Escrito")
        _add(db, case, now - timedelta(days=4), "Tenga presente", procedure="Escrito")
        _add(db, case, now - timedelta(days=3), "Acompaña documentos", procedure="Escrito")
        previo = _add(db, case, now - timedelta(days=1), PREVIO)

        _run(db, case)

        (alert,) = _alerts(db, case)
        assert alert.movement_id == previo.id

    def test_excepciones_after_the_resolution_does_not_count(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        now = datetime.now()
        _add(db, case, now - timedelta(days=2), PREVIO)
        _add(db, case, now - timedelta(days=1), "Opone excepciones", procedure="Escrito")

        _run(db, case)

        assert _alerts(db, case) == []

    def test_same_day_movement_with_lower_id_counts_as_previous(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        day = datetime.now() - timedelta(days=1)
        _add(db, case, day, "Opone excepciones", procedure="Escrito")
        _add(db, case, day, PREVIO)

        _run(db, case)

        assert len(_alerts(db, case)) == 1


class TestAlertsOnce:
    def test_alert_is_emitted_once_across_recomputes(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case)

        _run(db, case)
        _run(db, case)
        _run(db, case)

        assert len(_alerts(db, case)) == 1

    def test_a_new_previo_is_a_new_obligation_and_alerts_again(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        _excepciones_then_previo(db, case)
        _run(db, case)
        now = datetime.now()
        _add(db, case, now - timedelta(hours=2), "Opone excepciones", procedure="Escrito")
        second = _add(db, case, now, PREVIO)
        _add(db, case, now, "Resolución posterior")  # keep ordering unambiguous
        _run(db, case)

        assert sorted(a.movement_id for a in _alerts(db, case))[-1] == second.id
        assert len(_alerts(db, case)) == 2


class TestSemaforoUntouched:
    def test_alert_does_not_move_semaforo_or_next_deadline(self, db, case) -> None:
        _side(db, case, "AB.DDO")
        now = datetime.now()
        _add(db, case, now - timedelta(days=3), "Opone excepciones", procedure="Escrito")
        _run(db, case)  # baseline: no Previo yet
        baseline = (case.semaforo, case.next_deadline_at, case.next_deadline_fatal)

        _add(db, case, now - timedelta(days=1), PREVIO)
        _run(db, case)

        assert len(_alerts(db, case)) == 1  # the alert fired...
        assert (case.semaforo, case.next_deadline_at, case.next_deadline_fatal) == baseline
        assert case.next_deadline_fatal is False
        assert case.next_deadline_at != date.today()

    def test_alert_type_is_in_the_actionable_feed(self) -> None:
        from app.api.v1.alerts import ACTIONABLE_ALERT_TYPES

        assert ALERT_TYPE in ACTIONABLE_ALERT_TYPES

    def test_deadline_type_stays_informational(self) -> None:
        from app.core.deadlines_config import (
            ACTIONABLE_DEADLINE_VALUES, DeadlineType, INFORMATIONAL_DEADLINES,
        )

        assert DeadlineType.ACREDITAR_PODER_3D in INFORMATIONAL_DEADLINES
        assert "acreditar_poder_3d" not in ACTIONABLE_DEADLINE_VALUES
        assert DeadlineType.ACREDITAR_PODER_3D.is_fatal is True

    @pytest.mark.parametrize("side", ["demandado", "demandante"])
    def test_deadline_type_is_not_in_the_per_side_actionable_sets(self, side) -> None:
        from app.core.deadlines_config import actionable_sets

        mandatory, actionable = actionable_sets(side)
        assert "acreditar_poder_3d" not in mandatory
        assert "acreditar_poder_3d" not in actionable
