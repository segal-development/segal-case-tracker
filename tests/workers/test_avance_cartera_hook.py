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


@pytest.mark.parametrize("day,hour", [(2, 0), (3, 3), (5, 23)])
def test_days_2_to_5_catch_up_a_missed_day_one(day, hour):
    take, _ = _run(datetime(2026, 11, day, hour, 0))
    take.assert_called_once()
    assert take.call_args.args[2].isoformat() == f"2026-11-0{day}"


@pytest.mark.parametrize("day", [6, 15, 30])
def test_after_day_5_does_nothing(day):
    take, _ = _run(datetime(2026, 11, day, 3, 0))
    take.assert_not_called()


def test_catch_up_runs_once_per_day_but_again_next_day_if_failed():
    boom = MagicMock(side_effect=RuntimeError("down"))
    _run(datetime(2026, 11, 2, 3, 0), boom)
    take, _ = _run(datetime(2026, 11, 3, 3, 0))
    take.assert_called_once()


def test_disabled_flag_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_CAUSAS_SYNC_ENABLED", False, raising=False)
    with patch("app.services.sysgal_client.SysgalClient") as client_cls:
        take, _ = _run(datetime(2026, 11, 1, 3, 0))
    take.assert_not_called()
    client_cls.assert_not_called()


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


@pytest.mark.parametrize("now", [datetime(2026, 11, 1, 3, 0), datetime(2026, 11, 5, 3, 0), datetime(2026, 1, 2, 3, 0)])
def test_never_takes_a_photo_of_a_month_before_2026_11(now):
    """S11: days 1-5 of November 2026 would photograph October; the real service refuses it."""
    client = MagicMock()
    client.is_configured = True
    db = MagicMock()
    with patch("app.services.sysgal_client.SysgalClient", return_value=client), patch.object(
        ss, "_chile_now", return_value=now
    ), patch("app.services.avance_cartera.get_snapshot", return_value=None), patch(
        "app.services.avance_cartera.cierre_svc.is_cerrado", return_value=False
    ), patch("app.services.avance_cartera.take_snapshot") as take_snapshot:
        ss._maybe_take_avance_snapshot(db)
    client.estadisticas_cartera.assert_not_called()
    take_snapshot.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_takes_the_first_allowed_month_on_december_first():
    client = MagicMock()
    client.is_configured = True
    with patch("app.services.sysgal_client.SysgalClient", return_value=client), patch.object(
        ss, "_chile_now", return_value=datetime(2026, 12, 1, 3, 0)
    ), patch("app.services.avance_cartera.get_snapshot", return_value=None), patch(
        "app.services.avance_cartera.cierre_svc.is_cerrado", return_value=False
    ), patch("app.services.avance_cartera.take_snapshot") as take_snapshot:
        ss._maybe_take_avance_snapshot(MagicMock())
    take_snapshot.assert_called_once()
    assert take_snapshot.call_args.args[2] == "2026-11"
