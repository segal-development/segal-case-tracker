"""Circuit breaker for PJUD session rejections (worker-corta-ante-rechazos-de-sesion).

When PJUD keeps rejecting sessions that were just re-authenticated (the egress
IP is flagged), every further lawyer only burns another login. These tests pin:

* N consecutive "rejected right after a fresh re-auth" results stop the cycle,
* what counts, resets or is ignored by that counter,
* the in-memory cooldown that follows,
* the two settings that tune it.

``sync_lawyer_cases`` signals the case with ``auth_rejected_after_reauth: True``
in its failure dict (see ``AUTH_REJECTED_KEY``); no log text is parsed.
"""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.workers.sync_scheduler as scheduler
from app.config import Settings
from app.scrapper.pjud.exceptions import (
    SessionNotAuthenticatedError,
    ShapeChallengeError,
)

REJECTED = {
    "success": False,
    "error": "Not authenticated after session restore.",
    "auth_rejected_after_reauth": True,
}
OK = {"success": True, "cases_total": 1, "cases_new": 0}
SKIPPED = {"skipped": True, "reason": "no_session"}
NETWORK_FAIL = {"success": False, "error": "connection reset"}

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class FakeWall:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Telegram is a new channel these tests do not exercise: keep it off and
    make any accidental HTTP call fail loudly instead of reaching the network."""
    from app.config import settings as cfg
    from app.services import telegram_notifier

    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "")

    def _forbidden(*_a, **_k):
        raise AssertionError("unexpected real HTTP call")

    monkeypatch.setattr(telegram_notifier.httpx, "post", _forbidden)
    # The breaker now notifies on a trip; these tests pin the cycle, not the alert.
    monkeypatch.setattr(scheduler, "_notify_ip_block", lambda *_a, **_k: None)


@pytest.fixture
def wall(monkeypatch) -> FakeWall:
    fake = FakeWall()
    monkeypatch.setattr(scheduler, "_wall_now", fake)
    return fake


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(scheduler, "_previous_cycle", None)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_auth_block_until", None, raising=False)


async def _run_cycle(lawyer_ids, outcomes):
    """Run one cycle; ``outcomes`` maps call order to a result dict or exception."""
    queue = list(outcomes)

    async def impl(*_args, **_kwargs):
        out = queue.pop(0) if queue else OK
        if isinstance(out, Exception):
            raise out
        return out

    sync_mock = AsyncMock(side_effect=impl)
    # Maintenance work that needs no PJUD; exposed so a test can see it still ran.
    sysgal_mock = MagicMock(return_value={})
    sync_mock.sysgal = sysgal_mock
    sync_cls = MagicMock()
    sync_cls.return_value.needs_sync.return_value = True
    timer_holder = {}
    real_timer = scheduler.CycleTimer

    def capture_timer():
        timer_holder["t"] = real_timer()
        return timer_holder["t"]

    with patch.object(scheduler, "SessionLocal", side_effect=lambda: MagicMock()), \
         patch.object(scheduler, "_active_lawyer_ids_by_staleness", return_value=lawyer_ids), \
         patch.object(scheduler, "SyncService", sync_cls), \
         patch.object(scheduler, "COMPETENCIAS", ["civil"]), \
         patch.object(scheduler, "MAX_DATA_AGE_HOURS", 4), \
         patch.object(scheduler, "CycleTimer", capture_timer), \
         patch("app.services.credential_audit.scan_credential_changes", return_value=0), \
         patch("app.services.sysgal_sync.sync_sysgal_estados", sysgal_mock), \
         patch("app.services.scraping_health.revisar_y_avisar",
               return_value=MagicMock(requiere_aviso=False)), \
         patch.object(scheduler, "_maybe_take_cartera_snapshot"), \
         patch.object(scheduler.asyncio, "sleep", AsyncMock()), \
         patch.object(scheduler, "sync_lawyer_cases", sync_mock):
        await scheduler.sync_all_lawyers()
    return sync_mock, timer_holder["t"].results


def _attempted(sync_mock) -> list[int]:
    return [c.args[0] for c in sync_mock.await_args_list]


class TestBreakerTrips:
    @pytest.mark.asyncio
    async def test_three_consecutive_rejections_stop_the_cycle(self, wall, caplog):
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            mock, results = await _run_cycle(
                [1, 2, 3, 4, 5], [REJECTED, REJECTED, REJECTED]
            )

        assert _attempted(mock) == [1, 2, 3]
        assert results["aborted"] == 2
        assert results["failed"] == 3
        errors = [r for r in caplog.records if r.levelno == logging.ERROR
                  and "rejected" in r.getMessage().lower()]
        assert len(errors) == 1
        assert all(str(i) in errors[0].getMessage() for i in (1, 2, 3))

    @pytest.mark.asyncio
    async def test_a_success_between_rejections_resets_the_counter(self, wall):
        mock, results = await _run_cycle(
            [1, 2, 3, 4, 5], [REJECTED, REJECTED, OK, REJECTED, REJECTED]
        )

        assert _attempted(mock) == [1, 2, 3, 4, 5]
        assert results["aborted"] == 0

    @pytest.mark.asyncio
    async def test_a_skip_resets_the_counter(self, wall):
        mock, results = await _run_cycle(
            [1, 2, 3, 4, 5], [REJECTED, REJECTED, SKIPPED, REJECTED, REJECTED]
        )

        assert _attempted(mock) == [1, 2, 3, 4, 5]
        assert results["aborted"] == 0

    @pytest.mark.asyncio
    async def test_plain_failures_neither_count_nor_reset(self, wall):
        outcomes = [REJECTED, NETWORK_FAIL, REJECTED, RuntimeError("db down"), REJECTED]
        mock, results = await _run_cycle([1, 2, 3, 4, 5, 6], outcomes)

        assert _attempted(mock) == [1, 2, 3, 4, 5]
        assert results["aborted"] == 1

    @pytest.mark.asyncio
    async def test_fewer_rejections_than_the_threshold_do_not_trip(self, wall):
        mock, results = await _run_cycle([1, 2, 3], [REJECTED, REJECTED, OK])

        assert _attempted(mock) == [1, 2, 3]
        assert results["aborted"] == 0
        assert scheduler._auth_block_until is None

    @pytest.mark.asyncio
    async def test_shape_challenge_keeps_its_existing_behaviour(self, wall):
        shape = ShapeChallengeError("https://x", False, False, "TSPD")
        mock, results = await _run_cycle([1, 2, 3, 4], [shape, shape, shape])

        assert _attempted(mock) == [1, 2, 3, 4]
        assert results["failed"] == 3
        assert results["aborted"] == 0
        assert scheduler._auth_block_until is None

    @pytest.mark.asyncio
    async def test_the_threshold_comes_from_settings(self, wall, monkeypatch):
        monkeypatch.setattr(scheduler.settings, "SYNC_AUTH_REJECT_TRIP", 2)
        mock, results = await _run_cycle([1, 2, 3], [REJECTED, REJECTED])

        assert _attempted(mock) == [1, 2]
        assert results["aborted"] == 1


def _not_authenticated(cls=SessionNotAuthenticatedError):
    return cls("https://pjud.test/home", True, True)


async def _run_lawyer_sync(listing_effects) -> dict:
    """Drive the real ``sync_lawyer_cases`` with every collaborator mocked."""
    store = MagicMock()
    store.get_session_by_lawyer = AsyncMock(return_value=MagicMock(session_id="old"))
    store.adelete_session = AsyncMock(return_value=True)
    scraper = MagicMock()
    scraper.get_my_cases = AsyncMock(side_effect=listing_effects)
    scraper.close = AsyncMock()
    reauth = AsyncMock(return_value=(MagicMock(session_id="new"), None))
    with patch.object(scheduler, "get_session_store", return_value=store), \
         patch("app.api.v1.pjud.get_scraper", return_value=scraper), \
         patch.object(scheduler, "_reauth", reauth), \
         patch.object(scheduler, "SyncService") as sync_cls, \
         patch.object(scheduler, "_select_cases_for_detail_rotation", return_value=[]), \
         patch.object(scheduler, "detect_and_sync_movements",
                      AsyncMock(return_value=(0, 0, []))):
        sync_cls.return_value.sync_cases.return_value = MagicMock(cases_total=0, cases_new=0)
        return await scheduler.sync_lawyer_cases(1, "civil", MagicMock())


class TestRejectionSignal:
    @pytest.mark.asyncio
    async def test_rejection_right_after_a_fresh_reauth_is_flagged(self):
        result = await _run_lawyer_sync([_not_authenticated(), _not_authenticated()])

        assert result["success"] is False
        assert result["auth_rejected_after_reauth"] is True

    @pytest.mark.asyncio
    async def test_a_stale_session_that_recovers_is_a_plain_success(self):
        result = await _run_lawyer_sync([_not_authenticated(), []])

        assert result["success"] is True
        assert "auth_rejected_after_reauth" not in result

    @pytest.mark.asyncio
    async def test_a_plain_failure_is_not_flagged(self):
        result = await _run_lawyer_sync([RuntimeError("connection reset")])

        assert result["success"] is False
        assert "auth_rejected_after_reauth" not in result

    @pytest.mark.asyncio
    async def test_a_shape_challenge_is_not_flagged(self):
        result = await _run_lawyer_sync([ShapeChallengeError("u", False, False, "TSPD")])

        assert result["success"] is False
        assert "auth_rejected_after_reauth" not in result


class TestCooldown:
    @pytest.mark.asyncio
    async def test_next_cycle_inside_the_cooldown_does_nothing_and_logs_info(
        self, wall, caplog
    ):
        await _run_cycle([1, 2, 3], [REJECTED, REJECTED, REJECTED])
        wall.advance(minutes=89)

        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            mock, _ = await _run_cycle([1, 2, 3], [])

        mock.assert_not_awaited()
        infos = [r for r in caplog.records if r.levelno == logging.INFO
                 and "cooldown" in r.getMessage().lower()]
        assert infos

    @pytest.mark.asyncio
    async def test_maintenance_still_runs_during_the_cooldown(self, wall):
        """The cooldown only pauses PJUD: Sysgal, health and plazo alerts keep going."""
        await _run_cycle([1, 2, 3], [REJECTED, REJECTED, REJECTED])
        wall.advance(minutes=30)

        mock, _ = await _run_cycle([1, 2, 3], [])

        mock.assert_not_awaited()
        mock.sysgal.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_after_the_cooldown_runs_again(self, wall):
        await _run_cycle([1, 2, 3], [REJECTED, REJECTED, REJECTED])
        wall.advance(minutes=91)

        mock, results = await _run_cycle([1, 2], [])

        assert _attempted(mock) == [1, 2]
        assert results["aborted"] == 0
        assert scheduler._auth_block_until is None


class TestSettings:
    def test_defaults(self):
        s = Settings(_env_file=None)
        assert s.SYNC_AUTH_REJECT_TRIP == 3
        assert s.SYNC_AUTH_BLOCK_COOLDOWN_MINUTES == 90

    def test_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("SYNC_AUTH_REJECT_TRIP", "5")
        monkeypatch.setenv("SYNC_AUTH_BLOCK_COOLDOWN_MINUTES", "30")
        s = Settings(_env_file=None)
        assert s.SYNC_AUTH_REJECT_TRIP == 5
        assert s.SYNC_AUTH_BLOCK_COOLDOWN_MINUTES == 30
