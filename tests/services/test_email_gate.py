"""Tests for the outgoing e-mail gate (EMAIL_ALLOWED_KINDS / EMAIL_ALLOWED_RECIPIENTS)."""
import logging
from unittest.mock import patch

import pytest

from app.services import email_gate
from app.services.email_gate import email_allowed


@pytest.fixture(autouse=True)
def switch_off():
    """Env-rule tests run with the DB switch known to be off (no DB read)."""
    import time

    email_gate._switch_cache.update(value=False, at=time.monotonic())
    yield
    email_gate._switch_cache.clear()


@pytest.fixture
def gate():
    """Set the two gate settings for the duration of a test."""

    def _set(kinds: str = "", recipients: str = ""):
        return patch.multiple(
            email_gate.settings,
            EMAIL_ALLOWED_KINDS=kinds,
            EMAIL_ALLOWED_RECIPIENTS=recipients,
        )

    return _set


def test_empty_settings_allow_everything(gate):
    with gate():
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is True
        assert email_allowed("anything-unknown", "a@x.cl", "b@x.cl") is True


def test_kinds_only(gate):
    with gate(kinds="plazos_fatales_estudio"):
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, "a@x.cl") is True
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is False


def test_recipients_only(gate):
    with gate(recipients="carla@segal.cl"):
        assert email_allowed(email_gate.AGENDA, "carla@segal.cl") is True
        assert email_allowed(email_gate.AGENDA, "otro@segal.cl") is False


def test_both_must_hold(gate):
    with gate(kinds="plazos_fatales_estudio", recipients="carla@segal.cl"):
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, "carla@segal.cl") is True
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, "otro@segal.cl") is False
        assert email_allowed(email_gate.AGENDA, "carla@segal.cl") is False


def test_cc_counts_every_address(gate):
    with gate(recipients="carla@segal.cl"):
        assert email_allowed(email_gate.AGENDA, "carla@segal.cl", "otro@segal.cl") is False
        assert email_allowed(email_gate.AGENDA, "carla@segal.cl", "carla@segal.cl") is True


def test_case_and_spaces_are_ignored(gate):
    with gate(kinds=" Plazos_Fatales_Estudio , agenda ", recipients=" CARLA@Segal.cl ,x@y.cl"):
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, " Carla@SEGAL.cl ") is True
        assert email_allowed("AGENDA", "x@y.cl") is True


def test_unknown_kind_blocked_when_list_set(gate):
    with gate(kinds="agenda"):
        assert email_allowed("nope", "a@x.cl") is False


def test_kind_constants_values():
    assert email_gate.MOVIMIENTOS == "movimientos"
    assert email_gate.PLAZOS == "plazos"
    assert email_gate.PLAZOS_VERDE == "plazos_verde"
    assert email_gate.AGENDA == "agenda"
    assert email_gate.PLAZOS_FATALES_ESTUDIO == "plazos_fatales_estudio"
    assert email_gate.NOTIFICACIONES == "notificaciones"
    assert email_gate.SUPERVISOR == "supervisor"
    assert email_gate.SCRAPING == "scraping"


def test_suppression_logs_info_without_addresses(gate, caplog):
    with gate(kinds="agenda"), caplog.at_level(logging.INFO, logger="app.services.email_gate"):
        assert email_allowed("movimientos", "secreto@x.cl", "otro@x.cl") is False
    records = [r for r in caplog.records if r.name == "app.services.email_gate"]
    assert records and all(r.levelno == logging.INFO for r in records)
    text = " ".join(r.getMessage() for r in records)
    assert "movimientos" in text and "2" in text
    assert "secreto@x.cl" not in text


# --- Server-side kill switch stored in the DB (goals: correos_apagados) ---------------------


@pytest.fixture(autouse=False)
def fresh_switch():
    email_gate._switch_cache.clear()
    yield
    email_gate._switch_cache.clear()


def test_db_switch_on_blocks_every_kind_even_with_empty_env(gate, fresh_switch):
    with gate(), patch.object(email_gate, "_read_db_switch", return_value=True):
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is False
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, "a@x.cl") is False


def test_db_switch_off_leaves_the_env_rules(gate, fresh_switch):
    with gate(kinds="plazos_fatales_estudio"), patch.object(email_gate, "_read_db_switch", return_value=False):
        assert email_allowed(email_gate.PLAZOS_FATALES_ESTUDIO, "a@x.cl") is True
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is False


def test_db_switch_is_cached_between_calls(gate, fresh_switch):
    reader = patch.object(email_gate, "_read_db_switch", return_value=True)
    with gate(), reader as read:
        email_allowed(email_gate.MOVIMIENTOS, "a@x.cl")
        email_allowed(email_gate.AGENDA, "a@x.cl")
    assert read.call_count == 1


def test_db_read_failure_keeps_the_last_known_value(gate, fresh_switch):
    with gate(), patch.object(email_gate, "_read_db_switch", return_value=True):
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is False
    email_gate._switch_cache["at"] = 0.0  # expire the cache
    with gate(), patch.object(email_gate, "_read_db_switch", side_effect=RuntimeError("db down")):
        assert email_allowed(email_gate.MOVIMIENTOS, "a@x.cl") is False


def test_read_db_switch_uses_the_goals_row(db):
    from app.services import app_settings

    assert email_gate._read_db_switch(db) is False
    app_settings.set_int(db, app_settings.KEY_CORREOS_APAGADOS, 1)
    assert email_gate._read_db_switch(db) is True
