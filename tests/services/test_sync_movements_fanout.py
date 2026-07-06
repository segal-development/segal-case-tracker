"""Tests for SyncService.sync_movements's litigante-derived alert fan-out.

Task 1b-7 (unificar-modelo-causas, Approach C, ADR-005): a movement-alert
must be delivered to every abogado-of-record litigante on the case (via
``resolve_case_alert_recipients``), not just ``case.lawyer_id`` — one Alert
+ one notification dispatch per recipient, with NotifyBudget counted per
DISPATCH (not per case), and a firm-lawyer fallback when there are no
resolvable internal recipients (bootstrap window / opposing-counsel-only).
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.webhook import Webhook
from app.services.sync_service import NotifyBudget, ScrapedMovement, SyncService


def _movement(**kwargs):
    defaults = dict(
        folio="1",
        fecha="15/01/2026",
        tipo_tramite="Resolucion",
        descripcion="Se dicta resolucion",
        etapa="En tramitacion",
    )
    defaults.update(kwargs)
    return ScrapedMovement(**defaults)


def _add_litigante(db, case_id, participante, rut, nombre, suffix=""):
    lit = CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case_id}-{participante}-{rut}{suffix}",
    )
    db.add(lit)
    db.commit()
    return lit


@pytest.fixture
def firm_owned_case(db):
    firm = Lawyer(rut="16021492-9", name="Firm Lawyer", is_active=True)
    db.add(firm)
    db.flush()
    court = Court(code="T1-FANOUT", name="Juzgado Fanout Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(lawyer_id=firm.id, court_id=court.id, rol="C-1-2026", competencia="civil")
    db.add(case)
    db.commit()
    return {"firm": firm, "case": case}


class TestSyncMovementsAlertFanout:
    def test_movement_alert_delivered_to_all_abogados_of_record(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, case.id, "AP.DTE", marcela.rut, "Marcela")

        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            new_count, alert_count = sync.sync_movements(case.id, [_movement()])

        assert new_count == 1
        assert alert_count == 2  # one Alert per recipient

        alerts = db.query(Alert).filter(Alert.case_id == case.id).all()
        assert {a.lawyer_id for a in alerts} == {sandy.id, marcela.id}
        assert MockNotif.return_value.notify_new_movement.call_count == 2

    def test_no_delivery_to_non_litigante_owner(self, db, firm_owned_case):
        """The firm (case.lawyer_id) is NOT itself an abogado-of-record here
        — only Sandy is — so no alert routes to the firm lawyer_id."""
        firm = firm_owned_case["firm"]
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        db.add(sandy)
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")

        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService"):
            sync.sync_movements(case.id, [_movement()])

        alerts = db.query(Alert).filter(Alert.case_id == case.id).all()
        assert {a.lawyer_id for a in alerts} == {sandy.id}
        assert firm.id not in {a.lawyer_id for a in alerts}

    def test_notify_budget_counted_per_dispatch_not_per_case(self, db, firm_owned_case):
        """A shared NotifyBudget(limit=1) drains after the FIRST recipient's
        dispatch, not after the whole case — the second recipient's Alert is
        still persisted but its dispatch is skipped."""
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, case.id, "AP.DTE", marcela.rut, "Marcela")

        budget = NotifyBudget(limit=1)
        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            new_count, alert_count = sync.sync_movements(
                case.id, [_movement()], budget=budget
            )

        assert alert_count == 2  # both Alerts persisted regardless of budget
        assert MockNotif.return_value.notify_new_movement.call_count == 1
        assert budget.exhausted()

    def test_empty_recipients_falls_back_to_firm_lawyer(self, db, firm_owned_case):
        """No abogado-of-record litigante resolves yet (bootstrap window) —
        fall back to the case's existing owner lawyer so the alert is never
        silently dropped (preserves pre-Approach-C owner-alert behavior)."""
        firm = firm_owned_case["firm"]
        case = firm_owned_case["case"]
        # Only opposing/external counsel present — no internal Lawyer row.
        _add_litigante(db, case.id, "AB.DTE", "99999999-9", "Abogado Contrario")

        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            new_count, alert_count = sync.sync_movements(case.id, [_movement()])

        assert alert_count == 1
        alert = db.query(Alert).filter(Alert.case_id == case.id).first()
        assert alert.lawyer_id == firm.id
        assert MockNotif.return_value.notify_new_movement.call_count == 1

    def test_per_recipient_webhooks_fired(self, db, firm_owned_case):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, case.id, "AP.DTE", marcela.rut, "Marcela")

        sandy_hook = Webhook(
            lawyer_id=sandy.id, url="https://sandy.example/hook",
            events=["movement.created"], is_active=True,
        )
        marcela_hook = Webhook(
            lawyer_id=marcela.id, url="https://marcela.example/hook",
            events=["movement.created"], is_active=True,
        )
        db.add_all([sandy_hook, marcela_hook])
        db.commit()

        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            sync.sync_movements(case.id, [_movement()])

        calls = MockNotif.return_value.notify_new_movement.call_args_list
        assert len(calls) == 2
        webhooks_by_lawyer = {
            call.args[3].id: [w.id for w in call.args[4]] for call in calls
        }
        assert webhooks_by_lawyer[sandy.id] == [sandy_hook.id]
        assert webhooks_by_lawyer[marcela.id] == [marcela_hook.id]

    def test_no_duplicate_delivery_when_same_lawyer_has_two_litigante_rows(
        self, db, firm_owned_case
    ):
        case = firm_owned_case["case"]
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        db.add(sandy)
        db.commit()
        _add_litigante(db, case.id, "AB.DDO", sandy.rut, "Sandy", suffix="-1")
        _add_litigante(db, case.id, "AP.DTE", sandy.rut, "Sandy", suffix="-2")

        sync = SyncService(db)
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            new_count, alert_count = sync.sync_movements(case.id, [_movement()])

        assert alert_count == 1
        alerts = db.query(Alert).filter(Alert.case_id == case.id).all()
        assert len(alerts) == 1
        assert alerts[0].lawyer_id == sandy.id
        assert MockNotif.return_value.notify_new_movement.call_count == 1
