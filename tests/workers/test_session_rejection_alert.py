"""The session-rejection breaker tells the operator: one Telegram message per incident.

These tests drive the real ``sync_all_lawyers`` cycle with PJUD mocked and the
HTTP layer mocked. The notification must be best-effort: it never stops, breaks
or noticeably delays the cycle abort.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.workers.sync_scheduler as scheduler
from app.models.alerta_operativa import AlertaOperativa
from app.services import telegram_notifier

REJECTED = {
    "success": False,
    "error": "Not authenticated after session restore.",
    "auth_rejected_after_reauth": True,
}
OK = {"success": True, "cases_total": 1, "cases_new": 0}

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "123456:FAKE-TOKEN-NOT-REAL"
FAKE_CHAT = "-100987654321"
TIPO = "scraping_bloqueo_ip"


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


class FakeWall:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def wall(monkeypatch) -> FakeWall:
    fake = FakeWall()
    monkeypatch.setattr(scheduler, "_wall_now", fake)
    return fake


@pytest.fixture
def post(monkeypatch) -> MagicMock:
    fake = MagicMock(return_value=FakeResponse(200))
    monkeypatch.setattr(telegram_notifier.httpx, "post", fake)
    return fake


@pytest.fixture(autouse=True)
def _process_state(monkeypatch, post):
    monkeypatch.setattr(scheduler, "_previous_cycle", None)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_auth_block_until", None, raising=False)
    monkeypatch.setattr(telegram_notifier, "_disabled_warned", False, raising=False)
    monkeypatch.setattr(scheduler.settings, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(scheduler.settings, "TELEGRAM_CHAT_ID", FAKE_CHAT)


async def _run_cycle(db, lawyer_ids, outcomes):
    """One real cycle; every SessionLocal() hands out the test database."""
    queue = list(outcomes)

    async def impl(*_a, **_k):
        return queue.pop(0) if queue else OK

    sync_mock = AsyncMock(side_effect=impl)
    sync_cls = MagicMock()
    sync_cls.return_value.needs_sync.return_value = True
    holder = {}
    real_timer = scheduler.CycleTimer

    def capture_timer():
        holder["t"] = real_timer()
        return holder["t"]

    with patch.object(scheduler, "SessionLocal", side_effect=lambda: db), \
         patch.object(scheduler, "_active_lawyer_ids_by_staleness", return_value=lawyer_ids), \
         patch.object(scheduler, "SyncService", sync_cls), \
         patch.object(scheduler, "COMPETENCIAS", ["civil"]), \
         patch.object(scheduler, "MAX_DATA_AGE_HOURS", 4), \
         patch.object(scheduler, "CycleTimer", capture_timer), \
         patch.object(scheduler, "_run_semaforo_alerts_safely"), \
         patch("app.services.credential_audit.scan_credential_changes", return_value=0), \
         patch("app.services.sysgal_sync.sync_sysgal_estados", MagicMock(return_value={})), \
         patch("app.services.scraping_health.revisar_y_avisar",
               return_value=MagicMock(requiere_aviso=False)), \
         patch.object(scheduler, "_maybe_take_cartera_snapshot"), \
         patch.object(scheduler.asyncio, "sleep", AsyncMock()), \
         patch.object(scheduler, "sync_lawyer_cases", sync_mock):
        await scheduler.sync_all_lawyers()
    return sync_mock, holder["t"].results


def _attempted(mock) -> list[int]:
    return [c.args[0] for c in mock.await_args_list]


def _events(db):
    return db.query(AlertaOperativa).filter(AlertaOperativa.tipo == TIPO).all()


class TestBreakerNotifies:
    async def test_trip_sends_exactly_one_telegram_and_records_one_event(
        self, db, post, wall
    ):
        mock, results = await _run_cycle(db, [1, 2, 3, 4, 5], [REJECTED] * 3)

        assert _attempted(mock) == [1, 2, 3]
        assert results["aborted"] == 2
        post.assert_called_once()
        text = post.call_args.kwargs["json"]["text"]
        assert text == (
            "El PJUD rechazó 3 sesiones nuevas seguidas; la IP de la estación "
            "probablemente está bloqueada. El worker descansa 90 minutos."
        )
        assert len(_events(db)) == 1

    async def test_message_uses_the_configured_threshold_and_cooldown(
        self, db, post, wall, monkeypatch
    ):
        monkeypatch.setattr(scheduler.settings, "SYNC_AUTH_REJECT_TRIP", 2)
        monkeypatch.setattr(scheduler.settings, "SYNC_AUTH_BLOCK_COOLDOWN_MINUTES", 30)

        await _run_cycle(db, [1, 2, 3], [REJECTED] * 2)

        text = post.call_args.kwargs["json"]["text"]
        assert "rechazó 2 sesiones" in text
        assert "descansa 30 minutos" in text

    async def test_second_trip_inside_the_dedupe_window_sends_nothing(
        self, db, post, wall
    ):
        await _run_cycle(db, [1, 2, 3], [REJECTED] * 3)
        wall.advance(minutes=91)  # the breaker cooldown elapsed: the cycle runs again

        mock, results = await _run_cycle(db, [1, 2, 3], [REJECTED] * 3)

        assert _attempted(mock) == [1, 2, 3], "the second trip still stops the cycle"
        post.assert_called_once()
        assert len(_events(db)) == 1

    async def test_no_trip_no_message(self, db, post, wall):
        await _run_cycle(db, [1, 2, 3], [REJECTED, REJECTED, OK])

        post.assert_not_called()
        assert _events(db) == []

    async def test_cycle_inside_the_breaker_cooldown_sends_nothing(self, db, post, wall):
        await _run_cycle(db, [1, 2, 3], [REJECTED] * 3)
        wall.advance(minutes=30)

        await _run_cycle(db, [1, 2, 3], [])

        post.assert_called_once()


class TestNotificationIsBestEffort:
    async def test_a_raising_notifier_does_not_stop_the_abort(self, db, post, wall, caplog):
        with patch("app.services.scraping_health.avisar_bloqueo_ip",
                   side_effect=RuntimeError("notifier exploded")):
            with caplog.at_level(logging.INFO, logger="sync_scheduler"):
                mock, results = await _run_cycle(db, [1, 2, 3, 4, 5], [REJECTED] * 3)

        assert _attempted(mock) == [1, 2, 3]
        assert results["aborted"] == 2
        assert scheduler._auth_block_until is not None

    async def test_telegram_network_failure_does_not_stop_the_abort(self, db, post, wall):
        post.side_effect = httpx.ConnectError("down")

        mock, results = await _run_cycle(db, [1, 2, 3, 4], [REJECTED] * 3)

        assert _attempted(mock) == [1, 2, 3]
        assert scheduler._auth_block_until is not None
        assert len(_events(db)) == 1, "the event is persisted even if the message was not"

    async def test_telegram_not_configured_still_stops_and_persists(
        self, db, post, wall, monkeypatch
    ):
        monkeypatch.setattr(scheduler.settings, "TELEGRAM_BOT_TOKEN", "")

        mock, _ = await _run_cycle(db, [1, 2, 3, 4], [REJECTED] * 3)

        assert _attempted(mock) == [1, 2, 3]
        post.assert_not_called()
        assert len(_events(db)) == 1

    async def test_a_hanging_notifier_is_bounded_and_does_not_delay_the_cycle(
        self, db, post, wall, monkeypatch
    ):
        monkeypatch.setattr(scheduler, "_NOTIFY_TIMEOUT_SECONDS", 0.05, raising=False)

        def slow(*_a, **_k):
            time.sleep(0.6)
            return True

        with patch("app.services.scraping_health.avisar_bloqueo_ip", side_effect=slow):
            started = time.monotonic()
            mock, results = await _run_cycle(db, [1, 2, 3, 4], [REJECTED] * 3)
            elapsed = time.monotonic() - started

        assert _attempted(mock) == [1, 2, 3]
        assert results["aborted"] == 1
        assert elapsed < 0.4

    async def test_token_never_reaches_the_logs(self, db, post, wall, caplog):
        post.side_effect = httpx.ConnectError(
            f"failed https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
        )

        with caplog.at_level(logging.DEBUG):
            await _run_cycle(db, [1, 2, 3], [REJECTED] * 3)

        assert FAKE_TOKEN not in caplog.text
        assert all(FAKE_TOKEN not in str(r.__dict__) for r in caplog.records)
