"""The semaforo run: who is told what, when, and that nothing is lost or doubled.

Calendar: trigger Monday 2026-11-02 (see tests/unit/test_deadline_semaforo.py).
Day 3 = Thu 11-05, day 4 = Fri 11-06, day 6 = Mon 11-09, day 8 = Wed 11-11.
"""

from __future__ import annotations

import logging
import smtplib
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.semaforo_alerts import run_semaforo_alerts

TRIGGER = date(2026, 11, 2)
D = {1: date(2026, 11, 3), 2: date(2026, 11, 4), 3: date(2026, 11, 5),
     4: date(2026, 11, 6), 6: date(2026, 11, 9), 8: date(2026, 11, 11),
     9: date(2026, 11, 12)}
FIRM_RUT = "16021492-9"


@contextmanager
def smtp_ok():
    with patch("app.services.notification_service.settings") as st, patch(
        "app.services.notification_service.smtplib.SMTP"
    ) as mock_smtp:
        st.SMTP_HOST = "smtp.x.cl"; st.SMTP_PORT = 587; st.SMTP_USER = ""
        st.SMTP_PASSWORD = ""; st.SMTP_USE_TLS = False
        st.SMTP_FROM = "n@segal.cl"; st.FROM_EMAIL = "n@segal.cl"
        yield mock_smtp.return_value


@pytest.fixture
def people(db, monkeypatch):
    ana = Lawyer(rut=FIRM_RUT, name="Ana Pérez", email="ana@segal.cl", is_active=True)
    melissa = Lawyer(rut="11111111-1", name="Melissa Acevedo", email="melissa@segal.cl", is_active=True)
    carla = Lawyer(rut="22222222-2", name="Carla Lavín", email="carla@segal.cl", is_active=True, role="admin")
    db.add_all([ana, melissa, carla])
    db.flush()
    court = Court(code="SF-1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add(court)
    db.commit()
    monkeypatch.setattr(settings, "SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS", str(melissa.id))
    monkeypatch.setattr(settings, "SEMAFORO_ROJO_EXTRA_LAWYER_IDS", f"{melissa.id},{carla.id}")
    return {"ana": ana, "melissa": melissa, "carla": carla, "court": court}


def make_case(db, people, rol="C-1-2026", lawyer=None, status="active"):
    case = Case(
        lawyer_id=(lawyer or people["ana"]).id, court_id=people["court"].id, rol=rol,
        competencia="civil", plaintiff="Banco X", defendant="Deudor", status=status,
    )
    db.add(case)
    db.commit()
    return case


def make_deadline(db, case, type_="excepciones_8d", status="active", is_manual=False,
                  source_movement_id=None):
    days = 8 if type_ == "excepciones_8d" else 3
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, due_date=add_business_days(TRIGGER, days),
        triggered_at=TRIGGER, status=status, is_manual=is_manual,
        source_movement_id=source_movement_id,
    )
    db.add(row)
    db.commit()
    return row


def alerts(db, type_=None):
    q = db.query(Alert).filter(Alert.entity_type.like("plazo:%"))
    if type_:
        q = q.filter(Alert.type == type_)
    return q.order_by(Alert.id).all()


def who(db, type_):
    return sorted(db.get(Lawyer, a.lawyer_id).name for a in alerts(db, type_))


def subjects(smtp):
    return [c.args[0]["Subject"] for c in smtp.send_message.call_args_list]


class TestRecipientsByLevel:
    def test_verde_only_the_cases_lawyer_and_grouped(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            summary = run_semaforo_alerts(db, today=D[1])
        assert who(db, "plazo_verde") == ["Ana Pérez"]
        assert alerts(db, "plazo_amarillo") == [] and alerts(db, "plazo_rojo") == []
        assert smtp.send_message.call_count == 1
        assert subjects(smtp)[0].startswith("VERDE · 1 plazo nuevo")
        assert smtp.send_message.call_args.args[0]["To"] == "ana@segal.cl"
        assert summary["created"]["verde"] == 1

    def test_amarillo_adds_melissa_and_is_immediate(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[3])  # unconfirmed: 4-day reading is amarillo
            tos = sorted(c.args[0]["To"] for c in smtp.send_message.call_args_list)
        assert who(db, "plazo_amarillo") == ["Ana Pérez", "Melissa Acevedo"]
        assert tos == ["ana@segal.cl", "melissa@segal.cl"]  # carla is NOT told yet

    def test_rojo_adds_melissa_and_carla(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            tos = sorted(c.args[0]["To"] for c in smtp.send_message.call_args_list)
        assert who(db, "plazo_rojo") == ["Ana Pérez", "Carla Lavín", "Melissa Acevedo"]
        assert tos == ["ana@segal.cl", "carla@segal.cl", "melissa@segal.cl"]

    def test_a_lawyer_who_is_also_an_escalation_recipient_is_told_once(
        self, db, people
    ) -> None:
        make_deadline(db, make_case(db, people, lawyer=people["melissa"]))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            assert smtp.send_message.call_count == 2  # melissa once, carla once
        assert who(db, "plazo_rojo") == ["Carla Lavín", "Melissa Acevedo"]

    def test_amarillo_and_rojo_are_never_inside_the_verde_digest(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            assert not any(s.startswith("VERDE") for s in subjects(smtp))

    def test_each_rojo_is_its_own_email(self, db, people) -> None:
        make_deadline(db, make_case(db, people, rol="C-1-2026"))
        make_deadline(db, make_case(db, people, rol="C-2-2026"))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            assert smtp.send_message.call_count == 6  # 2 plazos x 3 people


class TestWhenAlertsFire:
    def test_a_second_run_the_same_day_does_nothing(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            first = smtp.send_message.call_count
            summary = run_semaforo_alerts(db, today=D[4])
            assert smtp.send_message.call_count == first
        assert len(alerts(db)) == 3
        assert summary["created"] == {"verde": 0, "amarillo": 0, "rojo": 0}

    def test_each_new_level_is_announced_once_as_days_pass(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            for day in (1, 2, 3, 4):
                run_semaforo_alerts(db, today=D[day])
        assert len(alerts(db, "plazo_verde")) == 1   # day 1; day 2 is the same slot+level
        assert len(alerts(db, "plazo_amarillo")) == 2
        assert len(alerts(db, "plazo_rojo")) == 3

    def test_the_same_level_returns_for_the_other_reading_and_is_announced_again(
        self, db, people
    ) -> None:
        """amarillo on day 3 (4-day reading) and again on day 6 (8-day reading)
        are two real alarms about two different possible deadlines."""
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[3])
            run_semaforo_alerts(db, today=D[6])
        assert len(alerts(db, "plazo_amarillo")) == 4  # (ana, melissa) x 2 slots
        assert {a.entity_type for a in alerts(db, "plazo_amarillo")} == {
            "plazo:excepciones_8d:u4+8", "plazo:excepciones_8d:u8",
        }

    def test_first_seen_late_gets_only_the_current_level_no_backlog(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert alerts(db, "plazo_verde") == [] and alerts(db, "plazo_amarillo") == []
        assert len(alerts(db, "plazo_rojo")) == 3

    def test_nothing_after_the_term_is_over(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[9])
            assert smtp.send_message.call_count == 0
        assert alerts(db) == []

    @pytest.mark.parametrize(
        "kwargs",
        [{"status": "superseded"}, {"status": "expired"}, {"is_manual": True}],
    )
    def test_only_active_automatic_rows(self, db, people, kwargs) -> None:
        make_deadline(db, make_case(db, people), **kwargs)
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert alerts(db) == []

    def test_archived_cases_are_left_alone(self, db, people) -> None:
        make_deadline(db, make_case(db, people, status="archived"))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert alerts(db) == []

    def test_when_the_firm_is_the_demandante_the_excepciones_window_is_not_ours(
        self, db, people
    ) -> None:
        case = make_case(db, people)
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DTE", rut=FIRM_RUT,
            persona_type="NATURAL", nombre="Ana", natural_key="k1",
        ))
        db.commit()
        make_deadline(db, case)
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert alerts(db) == []


class TestTheTermWeDoNotKnow:
    def test_unconfirmed_is_declared_in_the_stored_alert(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[3])
        a = alerts(db, "plazo_amarillo")[0]
        assert "sin confirmar" in a.title.lower()
        assert "SIN CONFIRMAR" in a.message
        assert "06-11-2026" in a.message and "11-11-2026" in a.message
        assert a.entity_type == "plazo:excepciones_8d:u4+8"

    def test_a_confirmed_term_is_stated_as_such(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.services.semaforo_alerts.confirmed_excepciones_term", lambda row: 8
        )
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[6])
        a = alerts(db, "plazo_amarillo")[0]
        assert "sin confirmar" not in a.title.lower()
        assert "Vence el 11-11-2026" in a.message
        assert a.entity_type == "plazo:excepciones_8d:8d"

    def test_a_confirmed_4_day_term_goes_rojo_on_day_4_and_is_over_on_day_5(
        self, db, people, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "app.services.semaforo_alerts.confirmed_excepciones_term", lambda row: 4
        )
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
            assert len(alerts(db, "plazo_rojo")) == 3
            run_semaforo_alerts(db, today=date(2026, 11, 7))
        assert len(alerts(db)) == 3

    def test_the_email_an_unconfirmed_plazo_sends_declares_it(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[4])
            msg = smtp.send_message.call_args_list[0].args[0]
        assert "sin confirmar" in msg["Subject"].lower()
        assert "SIN CONFIRMAR" in msg.get_body(("html",)).get_content()
        assert "SIN CONFIRMAR" in msg.get_body(("plain",)).get_content()


class TestNothingIsLost:
    def test_a_failed_verde_send_leaves_alerts_unmarked_then_the_next_run_resends(
        self, db, people
    ) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            run_semaforo_alerts(db, today=D[1])
            assert [a.email_sent for a in alerts(db, "plazo_verde")] == [False]
            smtp.send_message.side_effect = None
            smtp.send_message.reset_mock()
            run_semaforo_alerts(db, today=D[2])
            assert smtp.send_message.call_count == 1
        assert [a.email_sent for a in alerts(db, "plazo_verde")] == [True]
        assert len(alerts(db, "plazo_verde")) == 1  # resent, not re-created

    def test_a_failed_rojo_send_is_retried_by_the_next_run(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            run_semaforo_alerts(db, today=D[4])
            assert not any(a.email_sent for a in alerts(db, "plazo_rojo"))
            smtp.send_message.side_effect = None
            smtp.send_message.reset_mock()
            run_semaforo_alerts(db, today=D[4])
            assert smtp.send_message.call_count == 3
        assert all(a.email_sent for a in alerts(db, "plazo_rojo"))

    def test_an_alert_whose_term_is_over_is_not_retried(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            run_semaforo_alerts(db, today=D[8])
            smtp.send_message.side_effect = None
            smtp.send_message.reset_mock()
            run_semaforo_alerts(db, today=D[9])
            assert smtp.send_message.call_count == 0

    def test_urgent_goes_before_verde(self, db, people) -> None:
        c1 = make_case(db, people, rol="C-1-2026")
        make_deadline(db, c1)
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            run_semaforo_alerts(db, today=D[1])  # leaves a verde unsent
            smtp.send_message.side_effect = None
            smtp.send_message.reset_mock()
            c2 = make_case(db, people, rol="C-2-2026")
            row = make_deadline(db, c2)
            row.triggered_at = date(2026, 10, 28)  # an old plazo, now rojo
            row.due_date = add_business_days(row.triggered_at, 8)
            db.commit()
            run_semaforo_alerts(db, today=date(2026, 11, 3))
            first = subjects(smtp)[0]
        assert not first.startswith("VERDE")


class TestNeverBreaksTheCycle:
    def test_a_crash_in_assess_does_not_escape(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok(), patch(
            "app.services.semaforo_alerts.assess", side_effect=RuntimeError("boom")
        ):
            summary = run_semaforo_alerts(db, today=D[4])  # must not raise
        assert summary["errors"] == 1

    def test_one_bad_row_does_not_block_the_others(self, db, people) -> None:
        make_deadline(db, make_case(db, people, rol="C-1-2026"))
        make_deadline(db, make_case(db, people, rol="C-2-2026"))
        from app.services import semaforo_alerts as mod

        real = mod.assess
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(*a, **k)

        with smtp_ok(), patch.object(mod, "assess", side_effect=flaky):
            summary = run_semaforo_alerts(db, today=D[4])
        assert summary["errors"] == 1
        assert len(alerts(db, "plazo_rojo")) == 3

    def test_a_database_failure_before_the_loop_does_not_escape(self, db, people) -> None:
        with patch.object(db, "query", side_effect=RuntimeError("db down")):
            summary = run_semaforo_alerts(db, today=D[4])
        assert summary["errors"] >= 1

    def test_a_missing_or_garbled_recipient_is_logged_and_the_rest_still_get_it(
        self, db, people, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(
            settings, "SEMAFORO_ROJO_EXTRA_LAWYER_IDS", f"9999, abc, {people['carla'].id}"
        )
        make_deadline(db, make_case(db, people))
        with caplog.at_level(logging.WARNING), smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert who(db, "plazo_rojo") == ["Ana Pérez", "Carla Lavín"]
        assert "abc" in caplog.text and "9999" in caplog.text

    def test_an_inactive_escalation_recipient_is_skipped(self, db, people) -> None:
        people["carla"].is_active = False
        db.commit()
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert who(db, "plazo_rojo") == ["Ana Pérez", "Melissa Acevedo"]


class TestRatificacion:
    def _poder_case(self, db, people, *, ours=True):
        case = make_case(db, people)
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DDO", rut=FIRM_RUT,
            persona_type="NATURAL", nombre="Ana", natural_key="k-ddo",
        ))
        exc = Movement(
            case_id=case.id, stage="Excepciones", description="Opone excepciones",
            procedure="Escrito", movement_date=datetime(2026, 10, 30),
        )
        previo = Movement(
            case_id=case.id, stage="Inicio", description="Previo a proveer",
            procedure="Resolución", movement_date=datetime(2026, 11, 2),
        )
        db.add_all([exc, previo] if ours else [previo])
        db.commit()
        return case, previo

    def test_ours_follows_the_3_day_scale(self, db, people) -> None:
        case, previo = self._poder_case(db, people)
        make_deadline(db, case, "acreditar_poder_3d", source_movement_id=previo.id)
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[1])
            assert subjects(smtp)[0].startswith("VERDE")
            run_semaforo_alerts(db, today=D[2])
            run_semaforo_alerts(db, today=D[3])
        assert who(db, "plazo_amarillo") == ["Ana Pérez", "Melissa Acevedo"]
        assert who(db, "plazo_rojo") == ["Ana Pérez", "Carla Lavín", "Melissa Acevedo"]
        assert "tener por no presentado el escrito" in alerts(db, "plazo_rojo")[0].message

    def test_not_provably_ours_is_silent(self, db, people) -> None:
        case, previo = self._poder_case(db, people, ours=False)
        make_deadline(db, case, "acreditar_poder_3d", source_movement_id=previo.id)
        with smtp_ok() as smtp:
            run_semaforo_alerts(db, today=D[3])
            assert smtp.send_message.call_count == 0
        assert alerts(db) == []

    def test_over_after_day_3(self, db, people) -> None:
        case, previo = self._poder_case(db, people)
        make_deadline(db, case, "acreditar_poder_3d", source_movement_id=previo.id)
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert alerts(db) == []


class TestAlertTypes:
    def test_amarillo_and_rojo_are_actionable_verde_is_not(self) -> None:
        from app.api.v1.alerts import ACTIONABLE_ALERT_TYPES

        assert {"plazo_amarillo", "plazo_rojo"} <= ACTIONABLE_ALERT_TYPES
        assert "plazo_verde" not in ACTIONABLE_ALERT_TYPES

    def test_existing_actionable_types_are_untouched(self) -> None:
        from app.api.v1.alerts import ACTIONABLE_ALERT_TYPES

        assert {
            "semaforo_rojo", "deadline_fatal", "deadline_added", "deadline_audit",
            "credential_change", "status_change", "ratificar_firma",
        } <= ACTIONABLE_ALERT_TYPES
