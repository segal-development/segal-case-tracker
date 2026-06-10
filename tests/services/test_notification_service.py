"""
Tests for NotificationService - email (SendGrid) and webhook notifications.

TDD: written BEFORE implementation. All tests must fail initially.

Tests:
1. send_email_alert with empty SENDGRID_API_KEY -> False, no raise, alert untouched
2. send_email_alert happy path (SendGrid 202) -> True, alert.email_sent=True/at set
3. send_email_alert when SendGrid raises -> False, no raise
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
    def test_empty_api_key_returns_false_without_raising(self, service, caplog):
        """When SENDGRID_API_KEY is empty, return False, do not raise, log warning."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SENDGRID_API_KEY = ""
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with caplog.at_level(logging.WARNING, logger="app.services.notification_service"):
                result = service.send_email_alert(alert, lawyer)

        assert result is False
        assert alert.email_sent is False
        assert any("SENDGRID" in r.message.upper() or "api_key" in r.message.lower() or "key" in r.message.lower()
                   for r in caplog.records), "Expected a warning log about missing API key"

    def test_happy_path_202_sets_alert_fields(self, service):
        """SendGrid returns 202 -> True, alert.email_sent=True, email_sent_at set."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        mock_response = MagicMock()
        mock_response.status_code = 202

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SENDGRID_API_KEY = "SG.test_key_abc"
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with patch("app.services.notification_service.SendGridAPIClient") as MockSG:
                MockSG.return_value.send.return_value = mock_response

                result = service.send_email_alert(alert, lawyer)

        assert result is True
        assert alert.email_sent is True
        assert alert.email_sent_at is not None

    def test_sendgrid_raises_returns_false_without_raising(self, service):
        """If SendGridAPIClient.send() raises, return False without re-raising."""
        alert = _make_alert()
        lawyer = _make_lawyer()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.SENDGRID_API_KEY = "SG.test_key_abc"
            mock_settings.FROM_EMAIL = "from@segal.cl"

            with patch("app.services.notification_service.SendGridAPIClient") as MockSG:
                MockSG.return_value.send.side_effect = Exception("Network timeout")

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
