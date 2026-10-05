"""Engine behaviour of the parallel ACREDITAR_PODER_3D plazo.

It is a PARALLEL plazo (it coexists with excepciones in 3,415 real cases) so it
cannot go through ClassifierRule, which models "one state, one set of plazos".
These tests pin the invariants that matter: it survives state changes, never
supersedes/overwrites its neighbours, and never alerts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from app.api.v1.calendar import _deadline_labels_by_case
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine
from app.services.sync_service import NotifyBudget, emit_deadline_alerts

PREVIO = "Previo a proveer"
PODER = "acreditar_poder_3d"


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut="22222222-2", email="p@test.com", name="Poder Lawyer")
    court = Court(name="Tribunal Civil", code="TCP01", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-PODER-1",
        competencia="civil", filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _add(
    db, case: Case, when: datetime, description: str,
    stage: str = "Inicio de la Tramitación", procedure: str = "Resolución",
) -> Movement:
    mv = Movement(
        case_id=case.id, stage=stage, description=description,
        procedure=procedure, movement_date=when,
    )
    db.add(mv)
    db.flush()
    return mv


def _rows(db, case: Case, deadline_type: str | None = None) -> list[CaseDeadline]:
    q = db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id)
    if deadline_type:
        q = q.filter(CaseDeadline.deadline_type == deadline_type)
    return q.order_by(CaseDeadline.id).all()


def _make_demandante(db, case: Case) -> None:
    db.add(CaseLitigante(
        case_id=case.id, participante="AB.DTE", rut="22222222-2",
        persona_type="NATURAL", nombre="Poder Lawyer", natural_key=f"k{case.id}AB.DTE",
    ))
    db.flush()


class TestCreation:
    def test_previo_a_proveer_creates_plazo_3_business_days_from_its_date(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)  # Monday
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.status == "active"
        assert row.triggered_at == date(2026, 6, 1)
        assert row.due_date == date(2026, 6, 4)  # Thursday
        assert row.legal_basis == "art. 7 CPC · art. 7 Ley 20.886 (mod. Ley 21.394)"

    def test_saturday_counts_as_business_day(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 4), PREVIO)  # Thursday
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        # Fri(1) Sat(2) Mon(3); the Sunday is skipped. Excluding Saturday would give Tuesday 06-09.
        assert row.due_date == date(2026, 6, 8)

    def test_nulo_previo_a_proveer_creates_no_plazo(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), f"[Nulo] {PREVIO}")
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case, PODER) == []

    def test_real_case_wednesday_to_saturday_counting_saturday(self, db, case) -> None:
        """Real PJUD capture (Dirección Jurídica): resolución Previo a proveer on
        Wednesday 09/09/2026, Acredita Poder on Saturday 12/09/2026.

        Three días hábiles COUNTING the Saturday: Thu 10, Fri 11, Sat 12. It was
        due on the 12th and they complied on the 12th, the very last day. Without
        counting Saturday the numbers do not add up, so this validates
        business_days.py against a real case.
        """
        _add(db, case, datetime(2026, 9, 9), PREVIO)
        _add(db, case, datetime(2026, 9, 12), "Acredita Poder", procedure="Escrito")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.due_date == date(2026, 9, 12)
        assert row.status == "cumplido"

    def test_escrito_previo_a_proveer_creates_no_plazo(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO, procedure="Escrito")
        DeadlineEngine.recompute_case(db, case)
        assert _rows(db, case, PODER) == []

    def test_blank_procedure_previo_a_proveer_creates_plazo(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO, procedure="")
        DeadlineEngine.recompute_case(db, case)
        assert len(_rows(db, case, PODER)) == 1

    def test_recompute_is_idempotent(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        DeadlineEngine.recompute_case(db, case)
        assert len(_rows(db, case, PODER)) == 1


class TestCompliance:
    def test_later_acredita_poder_marks_cumplido(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        _add(db, case, datetime(2026, 6, 3), "Acredita Poder")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.status == "cumplido"

    def test_earlier_acredita_poder_does_not_mark_cumplido(self, db, case) -> None:
        _add(db, case, datetime(2026, 5, 20), "Acredita Poder")
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.status == "active"

    def test_compliance_arriving_on_a_later_sync_flips_active_to_cumplido(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        _add(db, case, datetime(2026, 6, 3), "Delega poder")
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.status == "cumplido"

    def test_auditor_no_cumplido_is_never_overwritten(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        row.status = "no_cumplido"
        db.flush()
        _add(db, case, datetime(2026, 6, 3), "Acredita Poder")
        DeadlineEngine.recompute_case(db, case)
        db.refresh(row)
        assert row.status == "no_cumplido"

    def test_annulled_previo_a_proveer_later_supersedes_the_plazo(self, db, case) -> None:
        """If the only apercibimiento disappears (annulled), the stale row must not stay active."""
        mv = _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        mv.description = f"[Nulo] {PREVIO}"
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        (row,) = _rows(db, case, PODER)
        assert row.status == "superseded"

    def test_new_previo_a_proveer_supersedes_the_previous_row(self, db, case) -> None:
        _add(db, case, datetime(2026, 6, 1), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        _add(db, case, datetime(2026, 6, 10), PREVIO)
        DeadlineEngine.recompute_case(db, case)
        old, new = _rows(db, case, PODER)
        assert (old.status, new.status) == ("superseded", "active")
        assert new.triggered_at == date(2026, 6, 10)


class TestParallelWithExcepciones:
    def _notification(self, db, case, when: datetime) -> None:
        _add(
            db, case, when,
            f"NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:{when:%d/%m/%Y} 10:00",
            stage="Gestión",
        )

    def test_poder_and_excepciones_coexist_without_overwriting_each_other(self, db, case) -> None:
        now = datetime.now()
        _add(db, case, now - timedelta(days=3), PREVIO)
        self._notification(db, case, now - timedelta(days=1))
        DeadlineEngine.recompute_case(db, case)
        statuses = {r.deadline_type: r.status for r in _rows(db, case)}
        assert statuses == {"excepciones_8d": "active", PODER: "active"}

    def test_poder_survives_the_case_advancing_state(self, db, case) -> None:
        """A live poder plazo must NOT be superseded when the state moves on.

        Step 5 of the engine supersedes every active row that is not in the
        classifier's current triggers. Advancing NOTIFICADO -> EXCEPCIONES drops
        excepciones_8d from the triggers; the poder row must stay active.
        """
        now = datetime.now()
        _add(db, case, now - timedelta(days=3), PREVIO)
        self._notification(db, case, now - timedelta(days=2))
        DeadlineEngine.recompute_case(db, case)
        assert case.procedural_state == "notificado"

        _add(db, case, now - timedelta(days=1), "Escrito", stage="Excepciones")
        DeadlineEngine.recompute_case(db, case)
        assert case.procedural_state == "excepciones"

        by_type = {r.deadline_type: r.status for r in _rows(db, case)}
        assert by_type[PODER] == "active"
        assert by_type["excepciones_8d"] == "superseded"  # control: step 5 still works for others


class TestNeverAlerts:
    def test_overdue_poder_does_not_move_semaforo_or_emit_alert(self, db, case) -> None:
        _make_demandante(db, case)
        now = datetime.now()
        _add(db, case, now - timedelta(days=60), "Ordena despachar mandamiento", stage="Mandamiento")
        _add(db, case, now - timedelta(days=30), PREVIO)  # long overdue, never fulfilled

        transition = DeadlineEngine.recompute_case(db, case)

        assert [r.status for r in _rows(db, case, PODER)] == ["active"]
        assert case.procedural_state == "mandamiento"
        assert case.semaforo == "verde"
        assert case.next_deadline_at is None
        assert case.next_deadline_fatal is False
        assert transition.entered_rojo is False
        assert transition.fatal_appeared is False
        with patch("app.services.sync_service.NotificationService") as notif:
            created = emit_deadline_alerts(db, case, transition, budget=NotifyBudget(5))
        assert created == 0
        assert db.query(Alert).filter(Alert.case_id == case.id).count() == 0
        notif.return_value.notify_deadline_alert.assert_not_called()


class TestNoLeakIntoOtherSurfaces:
    def test_calendar_label_ignores_a_poder_row_sharing_the_due_date(self, db, case) -> None:
        """next_deadline_at comes from an actionable plazo; the label must not borrow the poder row's."""
        due = date(2026, 7, 10)
        case.next_deadline_at = due
        db.add_all([
            CaseDeadline(  # lower id on purpose: it would win the "lowest id" tie-break
                case_id=case.id, deadline_type=PODER, due_date=due,
                triggered_at=date(2026, 7, 7), status="active",
            ),
            CaseDeadline(
                case_id=case.id, deadline_type="traslado_ejecutante_4d", due_date=due,
                triggered_at=date(2026, 7, 6), status="active",
            ),
        ])
        db.flush()
        assert _deadline_labels_by_case(db, [case]) == {case.id: "Traslado al ejecutante"}
