"""
Tests for NotificationService - email (SMTP) and webhook notifications.

Tests:
1. send_email_alert with empty SMTP_HOST -> False, no raise, alert untouched
2. send_email_alert happy path (SMTP send) -> True, alert.email_sent=True/at set
3. send_email_alert when SMTP raises -> False, no raise
4. send_webhook correct HMAC: canonical body + sha256 signature in header
5. send_webhook non-2xx -> False, webhook.failure_count incremented
6. send_webhook empty secret -> no X-Webhook-Signature header, warning logged
7. notify_new_movement -> both channels called, payload structure correct, alert.webhook_sent set
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from app.services.notification_service import NotificationService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Mock db session — keeps unit tests free of a real DB."""
    return MagicMock()


@pytest.fixture
def service(db):
    return NotificationService(db)


def _make_alert(**kwargs):
    alert = MagicMock()
    alert.id = 1
    alert.title = "Nuevo movimiento en C-123-2025"
    alert.message = "Resolución: Citación para oír sentencia"
    alert.email_sent = False
    alert.email_sent_at = None
    alert.webhook_sent = False
    alert.webhook_sent_at = None
    for k, v in kwargs.items():
        setattr(alert, k, v)
    return alert


def _make_lawyer(**kwargs):
    lawyer = MagicMock()
    lawyer.id = 99
    lawyer.email = "abogado@ejemplo.cl"
    for k, v in kwargs.items():
        setattr(lawyer, k, v)
    return lawyer


def _make_webhook(**kwargs):
    webhook = MagicMock()
    webhook.id = 10
    webhook.url = "https://example.com/webhook"
    webhook.secret = "super-secret-key"
    webhook.is_active = True
    webhook.failure_count = 0
    for k, v in kwargs.items():
        setattr(webhook, k, v)
    return webhook


def _make_case(**kwargs):
    court = MagicMock()
    court.name = "24º Juzgado Civil de Santiago"

    case = MagicMock()
    case.id = 1
    case.rol = "C-123-2025"
    case.court = court
    case.plaintiff = "BANCO ITAU"
    case.defendant = "FERNANDEZ GOMEZ"
    for k, v in kwargs.items():
        setattr(case, k, v)
    return case


def _make_movement(**kwargs):
    movement = MagicMock()
    movement.id = 5
    movement.folio = "42"
    movement.movement_date = datetime(2025, 6, 1, 10, 0, 0)
    movement.description = "Citación para oír sentencia"
    movement.stage = "Término Probatorio"
    for k, v in kwargs.items():
        setattr(movement, k, v)
    return movement


# ---------------------------------------------------------------------------
# send_email_alert
# ---------------------------------------------------------------------------


class TestSendEmailAlert:
    def test_no_smtp_host_returns_false_without_raising(self, service, caplog):
        """When SMTP_HOST is empty, return False, do not raise, log warning."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = ""
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with caplog.at_level(logging.WARNING, logger="app.services.notification_service"):
                result = service.send_email_alert(alert, lawyer)

        assert result is False
        assert alert.email_sent is False
        assert any("smtp" in r.message.lower() for r in caplog.records), \
            "Expected a warning log about missing SMTP config"

    def test_happy_path_sends_via_smtp_and_sets_alert_fields(self, service):
        """SMTP send succeeds -> True, alert.email_sent=True, email_sent_at set,
        and the message is sent (with TLS + login when configured)."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = "smtp.proveedor.cl"
            mock_settings.SMTP_PORT = 587
            mock_settings.SMTP_USER = "user@segal.cl"
            mock_settings.SMTP_PASSWORD = "secret"
            mock_settings.SMTP_USE_TLS = True
            mock_settings.SMTP_FROM = "notificaciones@segal.cl"
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with patch("app.services.notification_service.smtplib.SMTP") as MockSMTP:
                server = MockSMTP.return_value.__enter__.return_value
                result = service.send_email_alert(alert, lawyer)

        assert result is True
        assert alert.email_sent is True
        assert alert.email_sent_at is not None
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@segal.cl", "secret")
        server.send_message.assert_called_once()

    def test_smtp_raises_returns_false_without_raising(self, service):
        """If the SMTP send raises, return False without re-raising."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SMTP_HOST = "smtp.proveedor.cl"
            mock_settings.SMTP_PORT = 587
            mock_settings.SMTP_USER = "user@segal.cl"
            mock_settings.SMTP_PASSWORD = "secret"
            mock_settings.SMTP_USE_TLS = True
            mock_settings.SMTP_FROM = ""
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with patch("app.services.notification_service.smtplib.SMTP") as MockSMTP:
                MockSMTP.return_value.__enter__.return_value.send_message.side_effect = Exception("SMTP down")
                result = service.send_email_alert(alert, lawyer)

        assert result is False
        # The key assertion: no exception was propagated


# ---------------------------------------------------------------------------
# send_webhook
# ---------------------------------------------------------------------------


class TestSendWebhook:
    def test_correct_hmac_in_header_and_canonical_body(self, service):
        """Verify the HMAC is sha256(secret, canonical_body) and body is the exact JSON sent."""
        webhook = _make_webhook()
        payload = {"event": "movement.created", "data": {"z": 1, "a": 2}}

        # Compute expected values exactly as the service should
        expected_body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expected_sig = hmac.new(
            webhook.secret.encode(), expected_body.encode(), hashlib.sha256
        ).hexdigest()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.services.notification_service.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_httpx.Client.return_value.__enter__.return_value = mock_client
            mock_httpx.Client.return_value.__exit__.return_value = False
            mock_client.post.return_value = mock_response

            result = service.send_webhook(webhook, payload)

        assert result is True
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["content"] == expected_body, "Body must be the canonical JSON"
        assert call_kwargs["headers"]["X-Webhook-Signature"] == f"sha256={expected_sig}"

    def test_non_2xx_returns_false_and_increments_failure_count(self, service):
        """HTTP 500 -> False, webhook.failure_count += 1."""
        webhook = _make_webhook(failure_count=3)
        payload = {"event": "test"}

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("app.services.notification_service.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_httpx.Client.return_value.__enter__.return_value = mock_client
            mock_httpx.Client.return_value.__exit__.return_value = False
            mock_client.post.return_value = mock_response

            result = service.send_webhook(webhook, payload)

        assert result is False
        assert webhook.failure_count == 4

    def test_empty_secret_sends_without_signature_header(self, service, caplog):
        """Webhook with empty secret: POST without X-Webhook-Signature, log warning."""
        webhook = _make_webhook(secret="")
        payload = {"event": "test"}

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.services.notification_service.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_httpx.Client.return_value.__enter__.return_value = mock_client
            mock_httpx.Client.return_value.__exit__.return_value = False
            mock_client.post.return_value = mock_response

            with caplog.at_level(logging.WARNING, logger="app.services.notification_service"):
                result = service.send_webhook(webhook, payload)

        assert result is True
        call_kwargs = mock_client.post.call_args.kwargs
        assert "X-Webhook-Signature" not in call_kwargs["headers"]
        assert len(caplog.records) > 0, "Expected a warning about missing secret"


# ---------------------------------------------------------------------------
# notify_new_movement
# ---------------------------------------------------------------------------


class TestNotifyNewMovement:
    def test_dispatches_both_channels_and_sets_webhook_sent(self, service):
        """Both send_email_alert and send_webhook are called; payload structure is correct;
        alert.webhook_sent=True when a webhook succeeds."""
        alert = _make_alert()
        case = _make_case()
        movement = _make_movement()
        lawyer = _make_lawyer()
        webhook = _make_webhook()

        with patch.object(service, "send_email_alert", return_value=True) as mock_email:
            with patch.object(service, "send_webhook", return_value=True) as mock_wh:
                service.notify_new_movement(alert, case, movement, lawyer, [webhook])

        # Email channel
        mock_email.assert_called_once_with(alert, lawyer)

        # Webhook channel
        mock_wh.assert_called_once()
        _, call_payload = mock_wh.call_args.args
        assert call_payload["event"] == "movement.created"
        assert call_payload["version"] == "1"
        data = call_payload["data"]
        assert data["lawyer_id"] == lawyer.id
        assert data["case"]["rol"] == case.rol
        assert data["movement"]["folio"] == movement.folio
        assert data["movement"]["descripcion"] == movement.description
        assert data["movement"]["etapa"] == movement.stage
        assert str(movement.movement_date) in data["movement"]["fecha"]

        # Webhook succeeded -> alert marked
        assert alert.webhook_sent is True
        assert alert.webhook_sent_at is not None


# ---------------------------------------------------------------------------
# S2-T02: _dispatch helper
# ---------------------------------------------------------------------------


class TestDispatchHelper:
    def test_dispatch_calls_email_and_all_active_webhooks(self, service):
        """_dispatch must call send_email_alert once and send_webhook for each active webhook."""
        alert = _make_alert()
        lawyer = _make_lawyer()
        webhook1 = _make_webhook(id=10)
        webhook2 = _make_webhook(id=11)
        payload = {"event": "test.created", "version": "1", "data": {}}

        with patch.object(service, "send_email_alert", return_value=True) as mock_email:
            with patch.object(service, "send_webhook", return_value=True) as mock_wh:
                service._dispatch(alert, lawyer, [webhook1, webhook2], payload)

        mock_email.assert_called_once_with(alert, lawyer)
        assert mock_wh.call_count == 2
        # At least one webhook succeeded → alert.webhook_sent must be True
        assert alert.webhook_sent is True
        assert alert.webhook_sent_at is not None

    def test_dispatch_skips_inactive_webhooks(self, service):
        """_dispatch must not call send_webhook for webhooks where is_active=False."""
        alert = _make_alert()
        lawyer = _make_lawyer()
        inactive = _make_webhook(is_active=False)
        payload = {"event": "test.created", "version": "1", "data": {}}

        with patch.object(service, "send_email_alert", return_value=True):
            with patch.object(service, "send_webhook", return_value=True) as mock_wh:
                service._dispatch(alert, lawyer, [inactive], payload)

        mock_wh.assert_not_called()


# ---------------------------------------------------------------------------
# S2-T02: notify_new_notificacion
# ---------------------------------------------------------------------------


def _make_notificacion_row(**kwargs):
    row = MagicMock()
    row.rol = "C-123-2025"
    row.tipo_notif = "PERSONAL"
    row.fecha_tramite = None
    row.nombre = "FERNANDEZ"
    row.tramite = "Demanda"
    for k, v in kwargs.items():
        setattr(row, k, v)
    return row


def _make_escrito_row(**kwargs):
    row = MagicMock()
    row.tipo_escrito = "DEMANDA"
    row.solicitante = "BANCO ITAU"
    row.fecha_ingreso = None
    for k, v in kwargs.items():
        setattr(row, k, v)
    return row


def _make_exhorto_row(**kwargs):
    row = MagicMock()
    row.tipo_exhorto = "ACTIVO"
    row.rol_destino = "E-355-2026"
    row.tribunal_destino = "Juzgado Destino"
    for k, v in kwargs.items():
        setattr(row, k, v)
    return row


class TestNotifyNewNotificacion:
    def test_sends_email_via_send_email_alert(self, service):
        """notify_new_notificacion must call send_email_alert once."""
        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        notif_row = _make_notificacion_row()

        with patch.object(service, "send_email_alert", return_value=True) as mock_email:
            with patch.object(service, "send_webhook", return_value=True):
                service.notify_new_notificacion(case, notif_row, lawyer, [], alert)

        mock_email.assert_called_once_with(alert, lawyer)

    def test_event_string_is_notificacion_created(self, service):
        """Webhook payload must carry event='notificacion.created'."""
        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        webhook = _make_webhook()
        notif_row = _make_notificacion_row()

        captured_payloads = []

        def capture(wh, payload):
            captured_payloads.append(payload)
            return True

        with patch.object(service, "send_email_alert", return_value=True):
            with patch.object(service, "send_webhook", side_effect=capture):
                service.notify_new_notificacion(case, notif_row, lawyer, [webhook], alert)

        assert len(captured_payloads) == 1
        assert captured_payloads[0]["event"] == "notificacion.created"
        assert captured_payloads[0]["version"] == "1"
        assert "notificacion" in captured_payloads[0]["data"]


# ---------------------------------------------------------------------------
# S2-T02: notify_new_escrito
# ---------------------------------------------------------------------------


class TestNotifyNewEscrito:
    def test_sends_webhook_with_hmac_signature(self, service):
        """notify_new_escrito → canonical JSON body with X-Webhook-Signature header."""
        import json

        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        webhook = _make_webhook()
        escrito_row = _make_escrito_row()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(service, "send_email_alert", return_value=True):
            with patch("app.services.notification_service.httpx") as mock_httpx:
                mock_client = MagicMock()
                mock_httpx.Client.return_value.__enter__.return_value = mock_client
                mock_httpx.Client.return_value.__exit__.return_value = False
                mock_client.post.return_value = mock_response

                service.notify_new_escrito(case, escrito_row, lawyer, [webhook], alert)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args.kwargs
        body = json.loads(call_kwargs["content"])
        assert body["event"] == "escrito.created"
        assert body["version"] == "1"
        assert "escrito" in body["data"]
        assert "X-Webhook-Signature" in call_kwargs["headers"]

    def test_event_string_is_escrito_created(self, service):
        """Webhook payload event must be exactly 'escrito.created'."""
        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        webhook = _make_webhook()
        escrito_row = _make_escrito_row()

        captured = []

        def capture(wh, payload):
            captured.append(payload)
            return True

        with patch.object(service, "send_email_alert", return_value=True):
            with patch.object(service, "send_webhook", side_effect=capture):
                service.notify_new_escrito(case, escrito_row, lawyer, [webhook], alert)

        assert captured[0]["event"] == "escrito.created"


# ---------------------------------------------------------------------------
# S2-T02: notify_new_exhorto
# ---------------------------------------------------------------------------


class TestNotifyNewExhorto:
    def test_sends_webhook_with_hmac_and_correct_event(self, service):
        """notify_new_exhorto → event='exhorto.created', HMAC header present."""
        import json

        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        webhook = _make_webhook()
        exhorto_row = _make_exhorto_row()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(service, "send_email_alert", return_value=True):
            with patch("app.services.notification_service.httpx") as mock_httpx:
                mock_client = MagicMock()
                mock_httpx.Client.return_value.__enter__.return_value = mock_client
                mock_httpx.Client.return_value.__exit__.return_value = False
                mock_client.post.return_value = mock_response

                service.notify_new_exhorto(case, exhorto_row, lawyer, [webhook], alert)

        call_kwargs = mock_client.post.call_args.kwargs
        body = json.loads(call_kwargs["content"])
        assert body["event"] == "exhorto.created"
        assert body["version"] == "1"
        assert "exhorto" in body["data"]
        assert "X-Webhook-Signature" in call_kwargs["headers"]

    def test_event_string_is_exhorto_created(self, service):
        alert = _make_alert()
        case = _make_case()
        lawyer = _make_lawyer()
        exhorto_row = _make_exhorto_row()

        captured = []

        def capture(wh, payload):
            captured.append(payload)
            return True

        with patch.object(service, "send_email_alert", return_value=True):
            with patch.object(service, "send_webhook", side_effect=capture):
                service.notify_new_exhorto(case, exhorto_row, lawyer, [_make_webhook()], alert)

        assert captured[0]["event"] == "exhorto.created"
