"""Daily Sysgal causas sync hook in the scheduler's maintenance block."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.workers import sync_scheduler as ss


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ss, "_last_sysgal_causas_day", None, raising=False)
    monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "http://sysgal")
    monkeypatch.setattr(settings, "SYSGAL_API_KEY", "k")


def _run(day=datetime(2026, 11, 10, 12, 0), sync=None, enc=None):
    sync = sync or MagicMock(return_value={"causas": 3})
    enc = enc or MagicMock(return_value={"encoladas": 1})
    with patch("app.services.sysgal_causas.sync_sysgal_causas", sync), \
         patch("app.services.sysgal_incorporacion.encolar_incorporaciones", enc), \
         patch.object(ss, "_chile_now", return_value=day):
        ss._maybe_sync_sysgal_causas(MagicMock())
    return sync, enc


def test_setting_defaults_to_false():
    assert settings.model_fields["SYSGAL_CAUSAS_SYNC_ENABLED"].default is False


def test_disabled_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", False, raising=False)
    sync, enc = _run()
    sync.assert_not_called()
    enc.assert_not_called()


def test_enabled_syncs_then_enqueues_once_per_chile_day(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", True, raising=False)
    sync, enc = _run()
    sync.assert_called_once()
    enc.assert_called_once()

    sync2, enc2 = _run(day=datetime(2026, 11, 10, 22, 0))
    sync2.assert_not_called()
    enc2.assert_not_called()

    sync3, _ = _run(day=datetime(2026, 11, 11, 1, 0))
    sync3.assert_called_once()


def test_failure_is_isolated_and_retried_next_cycle(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", True, raising=False)
    boom = MagicMock(side_effect=RuntimeError("403"))
    _, enc = _run(sync=boom)  # must not raise
    enc.assert_not_called()

    sync, _ = _run()
    sync.assert_called_once()


def test_unconfigured_client_skips(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "")
    sync, _ = _run()
    sync.assert_not_called()
