"""Monthly avance-de-cartera snapshot hook in the scheduler's maintenance block."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.workers import sync_scheduler as ss

TAKE = "app.services.avance_cartera.take_previous_month_if_missing"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ss, "_last_avance_snapshot_day", None, raising=False)
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "http://sysgal")
    monkeypatch.setattr(settings, "SYSGAL_API_KEY", "k")


def _run(now, take=None):
    take = take or MagicMock(return_value="taken")
    db = MagicMock()
    with patch(TAKE, take), patch.object(ss, "_chile_now", return_value=now):
        ss._maybe_take_avance_snapshot(db)
    return take, db


def test_day_one_after_01h_takes_previous_month():
    take, _ = _run(datetime(2026, 11, 1, 1, 0))
    take.assert_called_once()
    assert take.call_args.args[2].isoformat() == "2026-11-01"


def test_runs_once_per_day():
    _run(datetime(2026, 11, 1, 2, 0))
    take, _ = _run(datetime(2026, 11, 1, 9, 0))
    take.assert_not_called()


def test_before_01h_does_nothing():
    take, _ = _run(datetime(2026, 11, 1, 0, 59))
    take.assert_not_called()


def test_other_days_do_nothing():
    take, _ = _run(datetime(2026, 11, 2, 3, 0))
    take.assert_not_called()


def test_disabled_flag_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", False, raising=False)
    take, _ = _run(datetime(2026, 11, 1, 3, 0))
    take.assert_not_called()


def test_unconfigured_skips(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "")
    take, _ = _run(datetime(2026, 11, 1, 3, 0))
    take.assert_not_called()


@pytest.mark.parametrize("result", ["exists", "closed"])
def test_exists_or_closed_counts_as_done(result):
    _run(datetime(2026, 11, 1, 3, 0), MagicMock(return_value=result))
    take, _ = _run(datetime(2026, 11, 1, 5, 0))
    take.assert_not_called()


def test_failure_is_isolated_and_retried_next_run():
    boom = MagicMock(side_effect=RuntimeError("403"))
    _, db = _run(datetime(2026, 11, 1, 3, 0), boom)  # must not raise
    db.rollback.assert_called()
    take, _ = _run(datetime(2026, 11, 1, 4, 0))
    take.assert_called_once()
