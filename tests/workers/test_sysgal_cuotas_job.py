"""Daily Sysgal cuotas sync hook in the scheduler's maintenance block."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.workers import sync_scheduler as ss


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ss, "_last_sysgal_cuotas_day", None, raising=False)
    monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "http://sysgal")
    monkeypatch.setattr(settings, "SYSGAL_API_KEY", "k")
    monkeypatch.setattr(settings, "SYSGAL_CUOTAS_SYNC_ENABLED", True, raising=False)


def _run(day=datetime(2026, 11, 18, 9, 0), sync=None):
    sync = sync or MagicMock(return_value={"insertados": 1})
    db = MagicMock()
    with patch("app.services.sysgal_cuotas.sync_sysgal_cuotas", sync), \
         patch.object(ss, "_chile_now", return_value=day):
        ss._maybe_sync_sysgal_cuotas(db)
    return sync, db


def test_setting_defaults_to_false():
    assert settings.model_fields["SYSGAL_CUOTAS_SYNC_ENABLED"].default is False


def test_disabled_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CUOTAS_SYNC_ENABLED", False)
    sync, _ = _run()
    sync.assert_not_called()


def test_waits_until_after_08_chile():
    sync, _ = _run(day=datetime(2026, 11, 18, 7, 59))
    sync.assert_not_called()
    sync, _ = _run(day=datetime(2026, 11, 18, 8, 0))
    sync.assert_called_once()


def test_runs_once_per_chile_day_and_commits():
    sync, db = _run()
    sync.assert_called_once()
    db.commit.assert_called_once()
    sync2, _ = _run(day=datetime(2026, 11, 18, 22, 0))
    sync2.assert_not_called()
    sync3, _ = _run(day=datetime(2026, 11, 19, 8, 30))
    sync3.assert_called_once()


def test_failure_is_isolated_rolled_back_and_retried():
    boom = MagicMock(side_effect=RuntimeError("403"))
    _, db = _run(sync=boom)  # must not raise
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    sync, _ = _run()
    sync.assert_called_once()


def test_unconfigured_client_skips(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_KEY", "")
    sync, _ = _run()
    sync.assert_not_called()
