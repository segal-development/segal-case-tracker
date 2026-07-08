"""Tests for sync_service.emit_deadline_alerts — the ROJO / fatal-deadline
alert emitter (highest-value gap: DeadlineEngine.recompute_case computed the
semáforo but never told anyone when a case turned critical).

Mirrors the existing sync_movements litigante-derived alert fan-out pattern
(ADR-005): resolve_case_alert_recipients → one Alert + one notification
dispatch per recipient, budget-gated, firm-lawyer fallback when no internal
recipient resolves yet. Transition-based — see SemaforoTransition — so an
already-rojo case that stays rojo does NOT re-alert.
"""

from datetime import date
from unittest.mock import patch

import pytest

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.deadline_engine import SemaforoTransition
from app.services.sync_service import NotifyBudget, emit_deadline_alerts


def _add_litigante(db, case_id, participante, rut, nombre):
    lit = CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case_id}-{participante}-{rut}",
    )
    db.add(lit)
    db.commit()
    return lit


@pytest.fixture
def firm_owned_case(db):
    firm = Lawyer(rut="16021492-9", name="Firm Lawyer", is_active=True)
    db.add(firm)
    db.flush()
    court = Court(code="T1-DEADLINE-ALERT", name="Juzgado Deadline Alert Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=firm.id,
        court_id=court.id,
        rol="C-9-2026",
        competencia="civil",
        next_deadline_at=date(2026, 7, 20),
    )
    db.add(case)
    db.commit()
    return {"firm": firm, "case": case}


def _rojo_entry_transition(old="verde"):
    return SemaforoTransition(
        old_semaforo=old, new_semaforo="rojo", old_fatal=False, new_fatal=False,
    )


def _no_transition():
    return SemaforoTransition(
        old_semaforo="rojo", new_semaforo="rojo", old_fatal=False, new_fatal=False,
    )


def _rojo_to_verde_transition():
    return SemaforoTransition(
        old_semaforo="rojo", new_semaforo="verde", old_fatal=False, new_fatal=False,
    )


def _fatal_appeared_transition():
    return SemaforoTransition(
        old_semaforo="verde", new_semaforo="verde", old_fatal=False, new_fatal=True,
    )


class TestEmitDeadlineAlertsTransitionGate:
    def test_green_to_rojo_creates_one_alert_per_recipient(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, case.id, "AP.DTE", marcela.rut, "Marcela")

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _rojo_entry_transition())

        assert alert_count == 2
        alerts = db.query(Alert).filter(Alert.case_id == case.id).all()
        assert {a.lawyer_id for a in alerts} == {sandy.id, marcela.id}
        assert all(a.type == "semaforo_rojo" for a in alerts)
        assert all(case.rol in a.title for a in alerts)
        assert MockNotif.return_value.notify_deadline_alert.call_count == 2

    def test_already_rojo_stays_rojo_no_alert(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        _add_litigante(db, case.id, "AB.DDO", "17111111-1", "Sandy")

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _no_transition())

        assert alert_count == 0
        assert db.query(Alert).filter(Alert.case_id == case.id).count() == 0
        MockNotif.return_value.notify_deadline_alert.assert_not_called()

    def test_rojo_to_verde_no_alert(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        _add_litigante(db, case.id, "AB.DDO", "17111111-1", "Sandy")

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _rojo_to_verde_transition())

        assert alert_count == 0
        assert db.query(Alert).filter(Alert.case_id == case.id).count() == 0
        MockNotif.return_value.notify_deadline_alert.assert_not_called()

    def test_fatal_appeared_creates_deadline_fatal_alert(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        db.add(sandy)
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _fatal_appeared_transition())

        assert alert_count == 1
        alert = db.query(Alert).filter(Alert.case_id == case.id).first()
        assert alert.type == "deadline_fatal"
        assert case.rol in alert.title
        assert MockNotif.return_value.notify_deadline_alert.call_count == 1

    def test_no_recipients_no_crash_no_alert(self, db, firm_owned_case):
        """No abogado-of-record litigante AND no resolvable firm-lawyer
        fallback (case.lawyer_id points nowhere useful here is not the
        scenario — this covers the case where resolve_case_alert_recipients
        returns [] and the firm fallback lawyer row itself is missing)."""
        case = firm_owned_case["case"]
        # No litigantes at all → resolve_case_alert_recipients returns [].
        # Fallback resolves to case.lawyer_id (the firm), which DOES exist,
        # so to genuinely hit the "no recipients at all" branch we point
        # case.lawyer_id at a non-existent row.
        case.lawyer_id = 999999
        db.add(case)
        db.commit()

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _rojo_entry_transition())

        assert alert_count == 0
        assert db.query(Alert).filter(Alert.case_id == case.id).count() == 0
        MockNotif.return_value.notify_deadline_alert.assert_not_called()

    def test_empty_recipients_falls_back_to_firm_lawyer(self, db, firm_owned_case):
        firm = firm_owned_case["firm"]
        case = firm_owned_case["case"]
        # Only opposing/external counsel present — no internal Lawyer row.
        _add_litigante(db, case.id, "AB.DTE", "99999999-9", "Abogado Contrario")

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, _rojo_entry_transition())

        assert alert_count == 1
        alert = db.query(Alert).filter(Alert.case_id == case.id).first()
        assert alert.lawyer_id == firm.id
        assert MockNotif.return_value.notify_deadline_alert.call_count == 1

    def test_notify_budget_exhausted_alert_persisted_dispatch_skipped(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, case.id, "AP.DTE", marcela.rut, "Marcela")

        budget = NotifyBudget(limit=1)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(
                db, case, _rojo_entry_transition(), budget=budget
            )

        assert alert_count == 2  # both Alerts persisted regardless of budget
        assert MockNotif.return_value.notify_deadline_alert.call_count == 1
        assert budget.exhausted()

    def test_no_transition_at_all_is_a_noop(self, db, firm_owned_case):
        """A green→amber move (neither entered_rojo nor fatal_appeared) must
        not touch the recipient/alert machinery at all."""
        case = firm_owned_case["case"]
        _add_litigante(db, case.id, "AB.DDO", "17111111-1", "Sandy")
        transition = SemaforoTransition(
            old_semaforo="verde", new_semaforo="amarillo",
            old_fatal=False, new_fatal=False,
        )

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            alert_count = emit_deadline_alerts(db, case, transition)

        assert alert_count == 0
        MockNotif.assert_not_called()
