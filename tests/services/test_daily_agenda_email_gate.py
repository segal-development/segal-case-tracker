"""daily_agenda honours the outgoing e-mail gate inside ``_send_email``."""
from unittest.mock import patch

import pytest

from app.services import daily_agenda, email_gate
from app.services.daily_agenda import _send_email, send_daily_calendar_emails
from tests.services.test_daily_agenda import (  # noqa: F401  (fixtures)
    TARGET_DAY,
    _make_alert,
    _make_case,
    carla,
    court,
    lawyer,
)


@pytest.fixture
def smtp():
    """SMTP configured, ``smtplib.SMTP`` replaced; yields the class mock."""
    with (
        patch.object(daily_agenda.settings, "SMTP_HOST", "smtp.proveedor.cl"),
        patch.object(daily_agenda.settings, "SMTP_USE_TLS", False),
        patch.object(daily_agenda.settings, "SMTP_USER", ""),
        patch("app.services.daily_agenda.smtplib.SMTP") as cls,
    ):
        yield cls


@pytest.fixture
def carla_only():
    with patch.multiple(
        email_gate.settings,
        EMAIL_ALLOWED_KINDS="plazos_fatales_estudio",
        EMAIL_ALLOWED_RECIPIENTS="carla@segal.cl",
    ):
        yield


def test_agenda_kind_suppressed(smtp, carla_only):
    assert _send_email("carla@segal.cl", "s", "<p>h</p>", "t", kind="agenda") is False
    smtp.assert_not_called()


def test_other_recipient_suppressed_even_with_allowed_kind(smtp, carla_only):
    ok = _send_email("ana@segal.cl", "s", "<p>h</p>", "t", kind="plazos_fatales_estudio")
    assert ok is False
    smtp.assert_not_called()


def test_cc_to_non_allowed_address_suppressed(smtp, carla_only):
    ok = _send_email(
        "carla@segal.cl", "s", "<p>h</p>", "t",
        cc=["ana@segal.cl"], kind="plazos_fatales_estudio",
    )
    assert ok is False
    smtp.assert_not_called()


def test_carla_urgent_digest_still_sent(smtp, carla_only):
    ok = _send_email(
        "carla@segal.cl", "s", "<p>h</p>", "t", kind="plazos_fatales_estudio"
    )
    assert ok is True
    smtp.return_value.__enter__.return_value.send_message.assert_called_once()


def test_orchestration_only_carla_digest_goes_out(
    db, court, carla, lawyer, smtp, carla_only  # noqa: F811
):
    from app.models.alert import Alert  # noqa: F401

    c = _make_case(db, court, "C-950-2026", owner=lawyer)
    _make_alert(db, lawyer, c, atype="deadline_fatal", title="Plazo fatal en causa C-950-2026")

    summary = send_daily_calendar_emails(db, TARGET_DAY)

    server = smtp.return_value.__enter__.return_value
    assert server.send_message.call_count == 1
    msg = server.send_message.call_args.args[0]
    assert msg["To"] == "carla@segal.cl"
    assert "Plazos fatales del estudio" in msg["Subject"]
    assert summary["carla_digest_sent"] is True
    assert summary["sent"] == 0
