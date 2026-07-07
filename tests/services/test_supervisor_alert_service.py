"""Tests for supervisor_alert_service — SMTP email alert when a lawyer's PJUD
credential (Clave Única / segunda clave) stops working.

TDD: written BEFORE implementation. All tests must fail initially.

Tests:
1. render_supervisor_credential_alert: subject format, HTML + text contain
   lawyer name/RUT/method, timestamp present, non-empty bodies.
2. send_supervisor_credential_alert: SMTP not configured -> no-op, no crash.
3. send_supervisor_credential_alert: happy path -> SMTP client used, message
   sent with correct headers, returns True.
4. send_supervisor_credential_alert: SMTP raises -> returns False, no crash.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import supervisor_alert_service as svc


def _make_lawyer(**kwargs):
    lawyer = MagicMock()
    lawyer.id = 7
    lawyer.name = "Juan Pérez"
    lawyer.rut = "12345678-9"
    lawyer.preferred_auth_method = "clave_unica"
    for k, v in kwargs.items():
        setattr(lawyer, k, v)
    return lawyer


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestRenderSupervisorCredentialAlert:
    def test_subject_format(self):
        lawyer = _make_lawyer(name="Juan Pérez")
        subject, html_body, text_body = svc.render_supervisor_credential_alert(
            lawyer, reason="reauth_failed: invalid credentials"
        )
        assert subject == "⚠️ Acción requerida: la Clave Única de Juan Pérez dejó de sincronizar"

    def test_html_body_contains_lawyer_details(self):
        lawyer = _make_lawyer(name="María Soto", rut="9876543-2")
        _subject, html_body, _text_body = svc.render_supervisor_credential_alert(
            lawyer, reason="invalid credentials"
        )
        assert "María Soto" in html_body
        assert "9876543-2" in html_body
        assert "Clave Única" in html_body
        assert len(html_body) > 0
        assert "<table" in html_body  # table-based layout

    def test_text_body_non_empty_and_contains_details(self):
        lawyer = _make_lawyer(name="María Soto", rut="9876543-2")
        _subject, _html_body, text_body = svc.render_supervisor_credential_alert(
            lawyer, reason="invalid credentials"
        )
        assert len(text_body) > 0
        assert "María Soto" in text_body
        assert "9876543-2" in text_body

    def test_includes_timestamp(self):
        lawyer = _make_lawyer()
        _subject, html_body, text_body = svc.render_supervisor_credential_alert(
            lawyer, reason="invalid credentials"
        )
        # Some plausible date/time marker must appear (year present at minimum).
        import datetime

        year = str(datetime.datetime.now().year)
        assert year in html_body
        assert year in text_body


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------


class TestSendSupervisorCredentialAlert:
    @pytest.mark.asyncio
    async def test_not_configured_returns_false_no_crash(self):
        lawyer = _make_lawyer()
        with patch.object(svc.settings, "SMTP_HOST", ""), patch.object(
            svc.settings, "SUPERVISOR_EMAIL", ""
        ):
            result = await svc.send_supervisor_credential_alert(lawyer, "invalid credentials")
        assert result is False

    @pytest.mark.asyncio
    async def test_happy_path_sends_via_smtp(self):
        lawyer = _make_lawyer()
        mock_server = MagicMock()
        mock_smtp_cm = MagicMock()
        mock_smtp_cm.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cm.__exit__ = MagicMock(return_value=None)

        with (
            patch.object(svc.settings, "SMTP_HOST", "smtp.example.com"),
            patch.object(svc.settings, "SUPERVISOR_EMAIL", "supervisor@segal.cl"),
            patch.object(svc.settings, "SMTP_USER", "user@example.com"),
            patch.object(svc.settings, "SMTP_PASSWORD", "secret"),
            patch("smtplib.SMTP", return_value=mock_smtp_cm) as mock_smtp_class,
        ):
            result = await svc.send_supervisor_credential_alert(lawyer, "invalid credentials")

        assert result is True
        mock_smtp_class.assert_called_once()
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@example.com", "secret")
        mock_server.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_smtp_error_returns_false_no_crash(self):
        lawyer = _make_lawyer()
        with (
            patch.object(svc.settings, "SMTP_HOST", "smtp.example.com"),
            patch.object(svc.settings, "SUPERVISOR_EMAIL", "supervisor@segal.cl"),
            patch("smtplib.SMTP", side_effect=OSError("connection refused")),
        ):
            result = await svc.send_supervisor_credential_alert(lawyer, "invalid credentials")

        assert result is False
