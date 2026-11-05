"""NotificationService honours the outgoing e-mail gate at ``_deliver``."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services import email_gate
from app.services.notification_service import NotificationService


@pytest.fixture
def service():
    return NotificationService(MagicMock())


def _alert():
    alert = MagicMock()
    alert.id = 1
    alert.title = "Titulo"
    alert.message = "Mensaje"
    alert.email_sent = False
    alert.email_sent_at = None
    alert.type = "semaforo_amarillo"
    alert.case = None
    return alert


def _lawyer(email="abogado@ejemplo.cl"):
    lawyer = MagicMock()
    lawyer.id = 99
    lawyer.email = email
    return lawyer


@contextmanager
def _smtp_and_gate(kinds="plazos_fatales_estudio", recipients="carla@segal.cl"):
    with patch("app.services.notification_service.settings") as s:
        s.SMTP_HOST = "smtp.proveedor.cl"
        s.SMTP_PORT = 587
        s.SMTP_USER = ""
        s.SMTP_USE_TLS = False
        s.SMTP_FROM = "n@segal.cl"
        s.FROM_EMAIL = "n@segal.cl"
        with (
            patch.multiple(
                email_gate.settings,
                EMAIL_ALLOWED_KINDS=kinds,
                EMAIL_ALLOWED_RECIPIENTS=recipients,
            ),
            patch("app.services.notification_service.smtplib.SMTP") as smtp,
        ):
            yield smtp


def test_send_email_alert_suppressed_leaves_alert_unmarked(service):
    alert = _alert()
    with _smtp_and_gate() as smtp:
        assert service.send_email_alert(alert, _lawyer()) is False
    smtp.assert_not_called()
    assert alert.email_sent is False
    assert alert.email_sent_at is None


def test_send_email_alert_allowed_still_sends(service):
    alert = _alert()
    with _smtp_and_gate(kinds="notificaciones", recipients="abogado@ejemplo.cl") as smtp:
        assert service.send_email_alert(alert, _lawyer()) is True
    smtp.return_value.send_message.assert_called_once()
    assert alert.email_sent is True


def test_movement_digest_suppressed_sends_nothing_and_marks_nothing(service):
    alert = _alert()
    bucket = MagicMock(total_movements=1, entries=[MagicMock(alert=alert)])
    with _smtp_and_gate(), patch(
        "app.services.movement_digest.render_movement_digest",
        return_value=("s", "<p>h</p>", "t"),
    ):
        assert service._send_digest(_lawyer(), bucket) is False
    assert alert.email_sent is False


def test_plazo_email_suppressed_sends_nothing_and_marks_nothing(service):
    alert = _alert()
    with _smtp_and_gate() as smtp, patch(
        "app.services.plazo_email.render_plazo_alert",
        return_value=("s", "<p>h</p>", "t"),
    ):
        assert service._send_plazo_email(alert, _lawyer()) is False
    smtp.assert_not_called()
    assert alert.email_sent is False


def test_plazo_verde_digest_suppressed_sends_nothing_and_marks_nothing(service):
    alert = _alert()
    bucket = MagicMock(total=1, entries=[MagicMock(alert=alert)])
    with _smtp_and_gate() as smtp, patch(
        "app.services.plazo_email.render_plazo_verde_digest",
        return_value=("s", "<p>h</p>", "t"),
    ):
        assert service._send_plazo_verde_digest(_lawyer(), bucket) is False
    smtp.assert_not_called()
    assert alert.email_sent is False


@pytest.mark.parametrize(
    "kind", ["movimientos", "plazos", "plazos_verde", "notificaciones"]
)
def test_deliver_requires_kind_and_gates_each(service, kind):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = "abogado@ejemplo.cl"
    with _smtp_and_gate() as smtp:
        assert service._deliver(msg, kind) is False
    smtp.assert_not_called()
