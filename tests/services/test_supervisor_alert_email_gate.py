"""The supervisor credential alert honours the outgoing e-mail gate."""
from unittest.mock import MagicMock, patch

import pytest

from app.services import email_gate
from app.services import supervisor_alert_service as svc


def _lawyer():
    lawyer = MagicMock()
    lawyer.id = 7
    lawyer.name = "Juan Perez"
    lawyer.rut = "12345678-9"
    lawyer.preferred_auth_method = "clave_unica"
    return lawyer


@pytest.mark.asyncio
async def test_supervisor_alert_suppressed_hands_nothing_to_smtp():
    with (
        patch.object(svc.settings, "SMTP_HOST", "smtp.example.com"),
        patch.object(svc.settings, "SUPERVISOR_EMAIL", "supervisor@segal.cl"),
        patch.object(svc.settings, "SMTP_USER", ""),
        patch.object(svc.settings, "SMTP_USE_TLS", False),
        patch.multiple(
            email_gate.settings,
            EMAIL_ALLOWED_KINDS="plazos_fatales_estudio",
            EMAIL_ALLOWED_RECIPIENTS="carla@segal.cl",
        ),
        patch("smtplib.SMTP") as smtp,
    ):
        result = await svc.send_supervisor_credential_alert(_lawyer(), "invalid")

    assert result is False
    smtp.assert_not_called()


@pytest.mark.asyncio
async def test_supervisor_alert_allowed_still_sends():
    with (
        patch.object(svc.settings, "SMTP_HOST", "smtp.example.com"),
        patch.object(svc.settings, "SUPERVISOR_EMAIL", "supervisor@segal.cl"),
        patch.object(svc.settings, "SMTP_USER", ""),
        patch.object(svc.settings, "SMTP_USE_TLS", False),
        patch.multiple(
            email_gate.settings,
            EMAIL_ALLOWED_KINDS="supervisor",
            EMAIL_ALLOWED_RECIPIENTS="supervisor@segal.cl",
        ),
        patch("smtplib.SMTP") as smtp,
    ):
        result = await svc.send_supervisor_credential_alert(_lawyer(), "invalid")

    assert result is True
    smtp.return_value.__enter__.return_value.send_message.assert_called_once()
