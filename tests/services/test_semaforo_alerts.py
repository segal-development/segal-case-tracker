"""The semaforo run: who is told what, when, and that nothing is lost or doubled.

Calendar: trigger Monday 2026-11-02 (see tests/unit/test_deadline_semaforo.py).
Day 3 = Thu 11-05, day 4 = Fri 11-06, day 6 = Mon 11-09, day 8 = Wed 11-11.
"""

from __future__ import annotations

import logging
import re
import smtplib
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from dataclasses import replace

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


class TestTriggerCutoff:
    """Plazos triggered before the cutoff raise no alert (they stay on the
    Plazos screen). Measured on ``triggered_at``; failing towards silence."""

    @staticmethod
    def _aged_deadline(db, case, age_days, today):
        row = CaseDeadline(
            case_id=case.id, deadline_type="excepciones_8d",
            due_date=add_business_days(today, 8),  # still "active and in the future"
            triggered_at=today - timedelta(days=age_days), status="active", is_manual=False,
        )
        db.add(row)
        db.commit()
        return row

    def test_a_plazo_triggered_3_days_ago_alerts(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 7)
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=D[3])  # trigger was 3 days earlier
        assert who(db, "plazo_amarillo") == ["Ana Pérez", "Melissa Acevedo"]
        assert summary["suppressed_by_cutoff"] == 0

    @pytest.mark.parametrize("day,color", [(1, "verde"), (3, "amarillo"), (4, "rojo")])
    def test_a_plazo_triggered_60_days_ago_alerts_in_no_color(
        self, db, people, monkeypatch, day, color
    ) -> None:
        from app.services import semaforo_alerts as mod

        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 7)
        real = mod.assess
        # The pure assessment would call a 60-day-old plazo "over"; force the
        # color so the cutoff itself is what is under test.
        monkeypatch.setattr(
            mod, "assess", lambda dt, trig, today, term=None: real(dt, TRIGGER, D[day], term)
        )
        today = D[day]
        self._aged_deadline(db, make_case(db, people), 60, today)
        with smtp_ok() as smtp:
            summary = run_semaforo_alerts(db, today=today)
            assert smtp.send_message.call_count == 0
        assert alerts(db) == []
        assert summary["created"][color] == 0
        assert summary["suppressed_by_cutoff"] == 1

    @pytest.mark.parametrize("sin_nivel", ["assessment_is_none", "level_is_none"])
    def test_a_plazo_with_no_level_does_not_alert(
        self, db, people, monkeypatch, sin_nivel
    ) -> None:
        """No level means no reading applies: stay silent, do not guess one.

        ``_candidates`` already drops plazos whose due_date has gone by, so
        this guard covers what slips past it: an unknown deadline type, or a
        term that cannot be computed. Without it the code would raise on
        ``a.level`` (None assessment) or KeyError on ``ALERT_TYPE_BY_LEVEL``,
        and a crash inside the cycle is exactly what must never happen.
        """
        from app.services import semaforo_alerts as mod

        today = D[1]
        self._aged_deadline(db, make_case(db, people), 1, today)
        real = mod.assess

        def _no_level(dt, trig, when, term=None):
            if sin_nivel == "assessment_is_none":
                return None
            a = real(dt, trig, when, term)
            return None if a is None else replace(a, readings=(), level=None)

        monkeypatch.setattr(mod, "assess", _no_level)
        with smtp_ok() as smtp:
            summary = run_semaforo_alerts(db, today=today)
            assert smtp.send_message.call_count == 0
        assert alerts(db) == []
        assert summary["errors"] == 0

    def test_null_triggered_at_does_not_alert_and_is_not_an_error(
        self, db, people, monkeypatch
    ) -> None:
        from types import SimpleNamespace

        from app.services import semaforo_alerts as mod

        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 7)
        ghost = SimpleNamespace(id=1, case_id=1, deadline_type="excepciones_8d", triggered_at=None)
        monkeypatch.setattr(mod, "_candidates", lambda db_, today: [ghost])
        with smtp_ok() as smtp:
            summary = run_semaforo_alerts(db, today=D[4])
            assert smtp.send_message.call_count == 0
        assert alerts(db) == []
        assert summary["errors"] == 0
        assert summary["suppressed_by_cutoff"] == 1

    def test_zero_means_no_cutoff(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 0)
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=D[4])
        assert len(alerts(db, "plazo_rojo")) == 3
        assert summary["suppressed_by_cutoff"] == 0

    def test_negative_also_means_no_cutoff(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", -5)
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        assert len(alerts(db, "plazo_rojo")) == 3

    def test_the_age_boundary_is_inclusive(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 2)
        make_deadline(db, make_case(db, people))  # triggered 2026-11-02
        with smtp_ok():
            on_edge = run_semaforo_alerts(db, today=D[2])    # age 2 -> kept
            past_edge = run_semaforo_alerts(db, today=D[3])  # age 3 -> suppressed
        assert on_edge["suppressed_by_cutoff"] == 0
        assert past_edge["suppressed_by_cutoff"] == 1

    def test_the_suppressed_count_is_logged(self, db, people, monkeypatch, caplog) -> None:
        from app.services import semaforo_alerts as mod

        # alembic's env.py runs fileConfig(), which disables every logger that
        # exists at that time; a migration test earlier in the full suite then
        # makes caplog blind to this module. Re-enable it for this test only.
        monkeypatch.setattr(mod.logger, "disabled", False)
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 7)
        case = make_case(db, people)
        self._aged_deadline(db, case, 60, D[4])
        self._aged_deadline(db, case, 90, D[4]).deadline_type = "acreditar_poder_3d"
        db.commit()
        with caplog.at_level(logging.INFO, logger="app.services.semaforo_alerts"), smtp_ok():
            run_semaforo_alerts(db, today=D[4])
        line = next((r.getMessage() for r in caplog.records if "cutoff" in r.getMessage()), "")
        assert line.startswith("semaforo:") and "2" in line and "7" in line

    def test_a_cutoff_below_the_longest_term_silences_the_8d_rojo(
        self, db, people, monkeypatch
    ) -> None:
        """Documents why the default is not 7: day 8 of an 8-business-day plazo
        falls 9 calendar days after the trigger."""
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 7)
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[8])
        assert alerts(db, "plazo_rojo") == []
        monkeypatch.setattr(settings, "SEMAFORO_MAX_TRIGGER_AGE_DAYS", 14)
        with smtp_ok():
            run_semaforo_alerts(db, today=D[8])
        assert len(alerts(db, "plazo_rojo")) == 3

    def test_the_default_keeps_a_live_8d_plazo_escalating_to_rojo(self, db, people) -> None:
        assert settings.SEMAFORO_MAX_TRIGGER_AGE_DAYS >= 14
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=D[8])
        assert len(alerts(db, "plazo_rojo")) == 3


# ---------------------------------------------------------------------------
# Expired-and-unfiled notice (parallel channel, not a semaforo level)
# ---------------------------------------------------------------------------

TODAY = date(2026, 11, 20)


def make_expired(db, case, *, due=None, verdict="no_cumplido", type_="excepciones_8d",
                 status="expired", triggered_at=None):
    due = due or TODAY - timedelta(days=3)
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, due_date=due,
        triggered_at=triggered_at or due - timedelta(days=12), status=status, is_manual=False,
        verdict=verdict,
    )
    db.add(row)
    db.commit()
    return row


class TestExpiredSelection:
    def _select(self, db, today=TODAY):
        from app.services.semaforo_alerts import expired_unfiled_deadlines

        return [r.id for r in expired_unfiled_deadlines(db, today)]

    @pytest.mark.parametrize("verdict", ["no_cumplido", "fuera_de_plazo"])
    def test_selects_a_fatal_plazo_the_firm_did_not_act_on(self, db, people, verdict) -> None:
        row = make_expired(db, make_case(db, people), verdict=verdict)
        assert self._select(db) == [row.id]

    @pytest.mark.parametrize("type_", ["excepciones_8d", "apelacion_5d"])
    def test_both_fatal_types_are_selected(self, db, people, type_) -> None:
        row = make_expired(db, make_case(db, people), type_=type_)
        assert self._select(db) == [row.id]

    @pytest.mark.parametrize("type_", ["acreditar_poder_3d", "sentencia_10d", "traslado_ejecutante_4d"])
    def test_non_fatal_types_are_not_selected(self, db, people, type_) -> None:
        make_expired(db, make_case(db, people), type_=type_)
        assert self._select(db) == []

    @pytest.mark.parametrize("verdict", [None, "", "cumplido", "registro_tardio"])
    def test_unknown_or_acted_verdicts_are_excluded(self, db, people, verdict) -> None:
        make_expired(db, make_case(db, people), verdict=verdict)
        assert self._select(db) == []

    def test_the_selection_goes_by_verdict_not_by_row_status(self, db, people) -> None:
        case = make_case(db, people)
        ids = [
            make_expired(db, case, status=s, triggered_at=TODAY - timedelta(days=20 + i)).id
            for i, s in enumerate(("expired", "superseded", "no_cumplido"))
        ]
        assert self._select(db) == ids

    def test_a_plazo_still_running_or_due_today_is_not_expired(self, db, people) -> None:
        case = make_case(db, people)
        make_expired(db, case, due=TODAY)
        make_expired(db, case, due=TODAY + timedelta(days=1))
        assert self._select(db) == []

    def test_only_active_cases(self, db, people) -> None:
        make_expired(db, make_case(db, people, status="archived"))
        assert self._select(db) == []

    def test_the_window_boundary_is_inclusive(self, db, people, monkeypatch) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_EXPIRED_WINDOW_DAYS", 30)
        case = make_case(db, people)
        inside = make_expired(db, case, due=TODAY - timedelta(days=30))
        make_expired(db, case, due=TODAY - timedelta(days=31))
        assert self._select(db) == [inside.id]

    @pytest.mark.parametrize("value", [0, -5])
    def test_zero_or_negative_means_no_window(self, db, people, monkeypatch, value) -> None:
        monkeypatch.setattr(settings, "SEMAFORO_EXPIRED_WINDOW_DAYS", value)
        row = make_expired(db, make_case(db, people), due=TODAY - timedelta(days=900))
        assert self._select(db) == [row.id]

    def test_the_default_window_is_30_days(self) -> None:
        assert settings.SEMAFORO_EXPIRED_WINDOW_DAYS == 30

    def test_selection_has_no_side_effects(self, db, people) -> None:
        make_expired(db, make_case(db, people))
        self._select(db)
        assert alerts(db) == []


def vencidos(db):
    return db.query(Alert).filter(Alert.type == "plazo_vencido").order_by(Alert.id).all()


class TestExpiredAlert:
    def test_one_alert_per_recipient_with_the_dedupe_key_shape(self, db, people) -> None:
        row = make_expired(db, make_case(db, people))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=TODAY)
        found = vencidos(db)
        assert sorted(db.get(Lawyer, a.lawyer_id).name for a in found) == [
            "Ana Pérez", "Carla Lavín", "Melissa Acevedo",
        ]
        assert {a.entity_type for a in found} == {"plazo:excepciones_8d:vencido"}
        assert {a.entity_id for a in found} == {row.id}
        assert summary["expired_created"] == 3

    def test_it_is_announced_once_ever(self, db, people) -> None:
        make_expired(db, make_case(db, people))
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
            second = run_semaforo_alerts(db, today=TODAY + timedelta(days=1))
            third = run_semaforo_alerts(db, today=TODAY + timedelta(days=2))
        assert len(vencidos(db)) == 3
        assert second["expired_created"] == 0 and third["expired_created"] == 0

    def test_each_expired_plazo_gets_its_own_alerts(self, db, people) -> None:
        make_expired(db, make_case(db, people, rol="C-1-2026"))
        make_expired(db, make_case(db, people, rol="C-2-2026"))
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
        assert len(vencidos(db)) == 6

    def test_the_text_states_the_absolute_date_and_the_case(self, db, people) -> None:
        make_expired(db, make_case(db, people, rol="C-77-2026"), due=date(2026, 11, 17))
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
        a = vencidos(db)[0]
        assert "C-77-2026" in a.title and "17-11-2026" in a.title
        assert "1º Juzgado Civil" in a.message
        assert "hoy" not in a.message.lower()

    def test_no_voseo_no_exclamations(self, db, people) -> None:
        make_expired(db, make_case(db, people))
        make_expired(db, make_case(db, people, rol="C-2-2026"), verdict="fuera_de_plazo")
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
        for a in vencidos(db):
            text = f"{a.title} {a.message}"
            assert "!" not in text
            for voseo in ("revisá", "verificá", "contactá", "tenés"):
                assert voseo not in text.lower()
            assert not re.search(r"\bsos\b", text.lower())

    def test_fuera_de_plazo_is_not_worded_as_a_missed_filing(self, db, people) -> None:
        make_expired(db, make_case(db, people), verdict="fuera_de_plazo")
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
        assert "posterioridad" in vencidos(db)[0].message

    def test_when_the_firm_is_the_demandante_excepciones_is_not_ours(self, db, people) -> None:
        case = make_case(db, people)
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DTE", rut=FIRM_RUT,
            persona_type="NATURAL", nombre="Ana", natural_key="k1",
        ))
        db.commit()
        make_expired(db, case)
        with smtp_ok():
            run_semaforo_alerts(db, today=TODAY)
        assert vencidos(db) == []

    def test_outside_the_window_or_unknown_verdict_creates_nothing(self, db, people) -> None:
        case = make_case(db, people)
        make_expired(db, case, due=TODAY - timedelta(days=45))
        make_expired(db, case, verdict=None, triggered_at=date(2026, 9, 1))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=TODAY)
        assert vencidos(db) == [] and summary["expired_created"] == 0

    def test_it_does_not_change_the_live_term_alerts(self, db, people) -> None:
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=D[4])
        assert summary["created"] == {"verde": 0, "amarillo": 0, "rojo": 3}
        assert vencidos(db) == []

    def test_a_failure_in_the_notice_never_escapes_and_live_alerts_still_go(
        self, db, people, monkeypatch
    ) -> None:
        def boom(*a, **k):
            raise RuntimeError("selection exploded")

        monkeypatch.setattr("app.services.semaforo_alerts.expired_unfiled_deadlines", boom)
        make_deadline(db, make_case(db, people))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=D[4])
        assert summary["errors"] >= 1
        assert summary["created"]["rojo"] == 3

    def test_one_bad_expired_row_does_not_block_the_others(self, db, people, monkeypatch) -> None:
        import app.services.semaforo_alerts as mod

        real = mod._recipients
        calls = {"n": 0}

        def flaky(db_, case, level):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("bad row")
            return real(db_, case, level)

        monkeypatch.setattr(mod, "_recipients", flaky)
        make_expired(db, make_case(db, people, rol="C-1-2026"))
        make_expired(db, make_case(db, people, rol="C-2-2026"))
        with smtp_ok():
            summary = run_semaforo_alerts(db, today=TODAY)
        assert summary["errors"] == 1 and len(vencidos(db)) == 3
