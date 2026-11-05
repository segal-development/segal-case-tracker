"""NotificationService + the semaforo: amarillo/rojo go out immediately, one per
alert; verde is grouped into ONE email per lawyer and is never silently lost.

The movement digest from #336/#337/#338 must keep working next to it."""

import smtplib
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from app.models.alert import Alert
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.notification_service import NotificationService


@contextmanager
def smtp_ok():
    with patch("app.services.notification_service.settings") as st, patch(
        "app.services.notification_service.smtplib.SMTP"
    ) as mock_smtp:
        st.SMTP_HOST = "smtp.x.cl"; st.SMTP_PORT = 587; st.SMTP_USER = ""
        st.SMTP_PASSWORD = ""; st.SMTP_USE_TLS = False
        st.SMTP_FROM = "n@segal.cl"; st.FROM_EMAIL = "n@segal.cl"
        yield mock_smtp.return_value


def _world(db, n_cases=2):
    lawyer = Lawyer(rut="16021492-9", name="Ana Pérez", email="ana@segal.cl", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code="PZ-1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add(court)
    db.flush()
    cases = [
        Case(
            lawyer_id=lawyer.id, court_id=court.id, rol=f"C-{n}-2026",
            competencia="civil", plaintiff="Banco X", defendant=f"Deudor {n}",
        )
        for n in range(1, n_cases + 1)
    ]
    db.add_all(cases)
    db.commit()
    return lawyer, cases


def _alert(db, lawyer, case, type_, title="T", message="Linea uno\nLinea dos"):
    a = Alert(
        lawyer_id=lawyer.id, case_id=case.id, type=type_, title=title,
        message=message, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class TestImmediate:
    def test_a_rojo_alert_goes_out_alone_and_marks_email_sent(self, db) -> None:
        lawyer, (case, _) = _world(db)
        alert = _alert(db, lawyer, case, "plazo_rojo", title="ROJO · Excepciones, causa C-1-2026")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.notify_plazo_alert(alert, lawyer, [])
            assert smtp.send_message.call_count == 1  # immediately, not at close()
            msg = smtp.send_message.call_args.args[0]
            svc.close()
            assert smtp.send_message.call_count == 1  # and never again in a digest
        assert msg["Subject"] == "ROJO · Excepciones, causa C-1-2026"
        assert msg["To"] == "ana@segal.cl"
        body = msg.get_body(("html",)).get_content()
        assert "Plazo en rojo" in body and "C-1-2026" in body and "Deudor 1" in body
        assert alert.email_sent is True and alert.email_sent_at is not None

    def test_two_amarillo_alerts_are_two_emails(self, db) -> None:
        lawyer, (c1, c2) = _world(db)
        a1 = _alert(db, lawyer, c1, "plazo_amarillo", title="AMARILLO · uno")
        a2 = _alert(db, lawyer, c2, "plazo_amarillo", title="AMARILLO · dos")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.notify_plazo_alert(a1, lawyer, [])
            svc.notify_plazo_alert(a2, lawyer, [])
            svc.close()
            subjects = [c.args[0]["Subject"] for c in smtp.send_message.call_args_list]
        assert subjects == ["AMARILLO · uno", "AMARILLO · dos"]

    def test_a_failed_immediate_send_leaves_the_alert_unmarked_and_does_not_raise(
        self, db
    ) -> None:
        lawyer, (case, _) = _world(db)
        alert = _alert(db, lawyer, case, "plazo_rojo")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            svc.notify_plazo_alert(alert, lawyer, [])
        assert not alert.email_sent

    def test_text_part_and_tuteo(self, db) -> None:
        lawyer, (case, _) = _world(db)
        alert = _alert(db, lawyer, case, "plazo_amarillo", title="AMARILLO · x")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.notify_plazo_alert(alert, lawyer, [])
            msg = smtp.send_message.call_args.args[0]
        text = msg.get_body(("plain",)).get_content()
        assert "Hola Ana Pérez" in text and "Linea uno" in text


class TestGroupedVerde:
    def test_many_verde_alerts_are_one_email_sent_at_close(self, db) -> None:
        lawyer, (c1, c2) = _world(db)
        a1 = _alert(db, lawyer, c1, "plazo_verde", title="VERDE · uno")
        a2 = _alert(db, lawyer, c1, "plazo_verde", title="VERDE · dos")
        a3 = _alert(db, lawyer, c2, "plazo_verde", title="VERDE · tres")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            for a in (a1, a2, a3):
                svc.accumulate_plazo_verde(a, lawyer)
            assert smtp.send_message.call_count == 0
            svc.close()
            assert smtp.send_message.call_count == 1
            msg = smtp.send_message.call_args.args[0]
        assert msg["Subject"] == "VERDE · 3 plazos nuevos en 2 causas"
        html_body = msg.get_body(("html",)).get_content()
        for t in ("VERDE · uno", "VERDE · dos", "VERDE · tres"):
            assert t in html_body
        assert all(a.email_sent and a.email_sent_at for a in (a1, a2, a3))

    def test_one_email_per_lawyer(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        other = Lawyer(rut="11111111-1", name="Beto", email="beto@segal.cl", is_active=True)
        db.add(other)
        db.commit()
        a1 = _alert(db, lawyer, c1, "plazo_verde")
        a2 = _alert(db, other, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.accumulate_plazo_verde(a1, lawyer)
            svc.accumulate_plazo_verde(a2, other)
            svc.close()
            tos = sorted(c.args[0]["To"] for c in smtp.send_message.call_args_list)
        assert tos == ["ana@segal.cl", "beto@segal.cl"]

    def test_a_failed_send_leaves_every_alert_unmarked_and_never_raises(self, db) -> None:
        lawyer, (c1, c2) = _world(db)
        a1 = _alert(db, lawyer, c1, "plazo_verde")
        a2 = _alert(db, lawyer, c2, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            svc.accumulate_plazo_verde(a1, lawyer)
            svc.accumulate_plazo_verde(a2, lawyer)
            svc.close()  # must not raise
        assert not a1.email_sent and not a2.email_sent

    def test_a_lawyer_without_email_is_skipped_and_unmarked(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        lawyer.email = None
        db.commit()
        a1 = _alert(db, lawyer, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.accumulate_plazo_verde(a1, lawyer)
            svc.close()
            assert smtp.send_message.call_count == 0
        assert not a1.email_sent

    def test_the_mark_is_durable_not_just_flushed(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        a1 = _alert(db, lawyer, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok():
            svc.accumulate_plazo_verde(a1, lawyer)
            svc.close()
        db.rollback()
        assert db.query(Alert).filter(Alert.id == a1.id).one().email_sent is True

    def test_a_421_is_retried_like_any_other_digest(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        a1 = _alert(db, lawyer, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            smtp.send_message.side_effect = [
                smtplib.SMTPResponseException(421, b"4.4.2 timeout"),
                None,
            ]
            svc.accumulate_plazo_verde(a1, lawyer)
            svc.close()
            assert smtp.send_message.call_count == 2
        assert a1.email_sent


class TestNextToTheMovementDigest:
    def _movement(self, db, lawyer, case):
        from app.models.movement import Movement

        mv = Movement(
            case_id=case.id, description="Resolucion X", procedure="Resolucion",
            movement_date=datetime(2026, 11, 2), folio="1",
        )
        db.add(mv)
        db.commit()
        return mv

    def test_movements_and_verde_are_two_separate_emails(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        mv_alert = _alert(db, lawyer, c1, "new_movement", title="Nuevo movimiento")
        mv = self._movement(db, lawyer, c1)
        verde = _alert(db, lawyer, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            svc.accumulate_movement(mv_alert, c1, mv, lawyer)
            svc.accumulate_plazo_verde(verde, lawyer)
            svc.close()
            subjects = sorted(c.args[0]["Subject"] for c in smtp.send_message.call_args_list)
        assert subjects == ["1 movimiento nuevo en 1 causa", "VERDE · 1 plazo nuevo en 1 causa"]
        assert mv_alert.email_sent and verde.email_sent

    def test_a_failing_verde_does_not_stop_the_movement_digest(self, db) -> None:
        lawyer, (c1, _) = _world(db)
        mv_alert = _alert(db, lawyer, c1, "new_movement")
        mv = self._movement(db, lawyer, c1)
        verde = _alert(db, lawyer, c1, "plazo_verde")
        svc = NotificationService(db)
        with smtp_ok() as smtp:
            def boom(msg, *a, **k):
                if msg["Subject"].startswith("VERDE"):
                    raise smtplib.SMTPRecipientsRefused({})
            smtp.send_message.side_effect = boom
            svc.accumulate_movement(mv_alert, c1, mv, lawyer)
            svc.accumulate_plazo_verde(verde, lawyer)
            svc.close()
        assert mv_alert.email_sent is True
        assert verde.email_sent is not True
