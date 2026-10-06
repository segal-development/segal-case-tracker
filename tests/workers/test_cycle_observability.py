"""Observability of the scraping cycle (no behaviour change).

The station works a fraction of the time and the log could not say why. These
tests pin the lines that answer it:

* why a lawyer was skipped (freshness guard) at a visible level,
* how the cycle time split across phases,
* how long since the previous cycle and when the next fire is due.

They also pin that none of that changes what the cycle actually does.
"""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.cycle_timing as cycle_timing
import app.workers.sync_scheduler as scheduler
from app.services.cycle_timing import timed


class FakeClock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cycle_timing, "_clock", fake)
    return fake


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """Each test starts as a freshly started worker with no scheduler."""
    monkeypatch.setattr(scheduler, "_previous_cycle", None)
    monkeypatch.setattr(scheduler, "_scheduler", None)


def _sync_service_cls(*, needs_sync, age=None):
    cls = MagicMock()
    cls.return_value.needs_sync.side_effect = needs_sync if callable(needs_sync) else None
    if not callable(needs_sync):
        cls.return_value.needs_sync.return_value = needs_sync
    if isinstance(age, Exception):
        cls.return_value.last_sync_age_hours.side_effect = age
    else:
        cls.return_value.last_sync_age_hours.return_value = age
    return cls


async def _run_cycle(lawyer_ids, sync_cls, sync_impl=None, sleep=None):
    sleep = sleep or AsyncMock()
    sync_mock = AsyncMock(side_effect=sync_impl) if sync_impl else AsyncMock(
        return_value={"success": True, "cases_total": 1, "cases_new": 0}
    )
    with patch.object(scheduler, "SessionLocal", side_effect=lambda: MagicMock()), \
         patch.object(scheduler, "_active_lawyer_ids_by_staleness",
                      **({"side_effect": lawyer_ids} if callable(lawyer_ids)
                         else {"return_value": lawyer_ids})), \
         patch.object(scheduler, "SyncService", sync_cls), \
         patch.object(scheduler, "COMPETENCIAS", ["civil"]), \
         patch.object(scheduler, "MAX_DATA_AGE_HOURS", 4), \
         patch("app.services.credential_audit.scan_credential_changes", return_value=0), \
         patch("app.services.sysgal_sync.sync_sysgal_estados", return_value={}), \
         patch("app.services.scraping_health.revisar_y_avisar",
               return_value=MagicMock(requiere_aviso=False)), \
         patch.object(scheduler, "_maybe_take_cartera_snapshot"), \
         patch.object(scheduler.asyncio, "sleep", sleep), \
         patch.object(scheduler, "sync_lawyer_cases", sync_mock):
        await scheduler.sync_all_lawyers()
    return sync_mock, sleep


def _messages(caplog, startswith: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(startswith)]


class TestFreshSkipIsVisible:
    @pytest.mark.asyncio
    async def test_skipped_for_freshness_is_logged_at_a_visible_level_with_the_reason(
        self, caplog
    ):
        sync_cls = _sync_service_cls(needs_sync=False, age=1.5)

        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle([7], sync_cls)

        skips = _messages(caplog, "Skipping lawyer 7")
        assert len(skips) == 1, [r.getMessage() for r in caplog.records]
        record = skips[0]
        assert record.levelno >= logging.INFO
        msg = record.getMessage()
        assert "civil" in msg
        assert "fresh" in msg
        assert "1.5h" in msg      # how old the last sync is
        assert "4h" in msg        # the freshness limit
        assert "2.5h" in msg      # how long until it stops being fresh

    @pytest.mark.asyncio
    async def test_a_failing_age_lookup_never_changes_the_skip(self, caplog):
        sync_cls = _sync_service_cls(needs_sync=False, age=RuntimeError("db down"))

        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            sync_mock, _ = await _run_cycle([7], sync_cls)

        sync_mock.assert_not_awaited()
        assert len(_messages(caplog, "Skipping lawyer 7")) == 1


class TestNoBehaviourChange:
    @pytest.mark.asyncio
    async def test_fresh_lawyer_is_still_skipped_and_stale_one_still_synced_at_same_pace(self):
        sync_cls = _sync_service_cls(
            needs_sync=lambda lawyer_id, competencia, max_age: lawyer_id != 1, age=1.0
        )

        sync_mock, sleep = await _run_cycle([1, 2], sync_cls)

        assert [c.args[:2] for c in sync_mock.await_args_list] == [(2, "civil")]
        # Cadence between work units is untouched: one 2 s pause per synced unit,
        # none for the skipped one.
        assert [c.args[0] for c in sleep.await_args_list] == [2]


class TestCycleSummary:
    @pytest.mark.asyncio
    async def test_summary_line_reports_where_the_time_went(self, caplog, clock):
        async def fake_sync(lawyer_id, competencia, db):
            with timed("listing"):
                clock.advance(60)
            with timed("detail"):
                clock.advance(100)
                with timed("documents"):
                    clock.advance(40)
                with timed("email"):
                    clock.advance(20)
            clock.advance(30)  # nobody measured this
            return {"success": True}

        sync_cls = _sync_service_cls(needs_sync=True)
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle([1], sync_cls, sync_impl=fake_sync)

        summary = _messages(caplog, "Cycle summary")
        assert len(summary) == 1
        msg = summary[0].getMessage()
        assert "total=4m10s" in msg
        assert "listing=1m00s" in msg
        assert "detail=1m40s" in msg      # exclusive of documents and email
        assert "documents=40s" in msg
        assert "email=20s" in msg
        assert "other=30s" in msg
        assert "synced=1" in msg

    @pytest.mark.asyncio
    async def test_summary_is_logged_even_when_the_cycle_crashes(self, caplog, clock):
        async def boom(lawyer_id, competencia, db):
            raise KeyboardInterrupt  # not an Exception: escapes the per-unit handler

        sync_cls = _sync_service_cls(needs_sync=True)
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            with pytest.raises(KeyboardInterrupt):
                await _run_cycle([1], sync_cls, sync_impl=boom)

        assert len(_messages(caplog, "Cycle summary")) == 1

    @pytest.mark.asyncio
    async def test_summary_counts_fresh_lawyers(self, caplog):
        sync_cls = _sync_service_cls(needs_sync=False, age=1.0)
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle([1, 2, 3], sync_cls)

        msg = _messages(caplog, "Cycle summary")[0].getMessage()
        assert "fresh=3" in msg
        assert "synced=0" in msg


class TestCycleGaps:
    @staticmethod
    def _wall(monkeypatch, start):
        state = {"now": start}
        monkeypatch.setattr(scheduler, "_wall_now", lambda: state["now"])
        return state

    @pytest.mark.asyncio
    async def test_start_line_reports_gap_since_previous_cycle_and_next_fire(
        self, caplog, monkeypatch
    ):
        t0 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        wall = self._wall(monkeypatch, t0)

        job = MagicMock()
        job.next_run_time = datetime(2026, 10, 6, 14, 5, tzinfo=timezone.utc)
        fake_scheduler = MagicMock()
        fake_scheduler.get_job.return_value = job
        monkeypatch.setattr(scheduler, "_scheduler", fake_scheduler)

        def advance_30_min(*a, **k):
            wall["now"] = t0 + timedelta(minutes=30)
            return []

        sync_cls = _sync_service_cls(needs_sync=True)
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle(advance_30_min, sync_cls)
            first = _messages(caplog, "Cycle start")[0].getMessage()

            wall["now"] = t0 + timedelta(hours=2, minutes=5)
            await _run_cycle([], sync_cls)
            second = _messages(caplog, "Cycle start")[1].getMessage()

        assert "first cycle since worker start" in first
        assert "previous cycle started 2h05m ago" in second
        assert "ended 1h35m ago" in second
        assert "next fire 2026-10-06 14:05" in second

    @pytest.mark.asyncio
    async def test_start_line_without_a_scheduler_says_next_fire_is_unknown(
        self, caplog, monkeypatch
    ):
        self._wall(monkeypatch, datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc))
        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle([], _sync_service_cls(needs_sync=True))

        assert "next fire unknown" in _messages(caplog, "Cycle start")[0].getMessage()

    @pytest.mark.asyncio
    async def test_summary_flags_that_the_next_fire_is_already_overdue(
        self, caplog, monkeypatch
    ):
        t0 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        wall = self._wall(monkeypatch, t0)
        job = MagicMock()
        job.next_run_time = t0 + timedelta(hours=2)  # due 2 h after the start
        fake_scheduler = MagicMock()
        fake_scheduler.get_job.return_value = job
        monkeypatch.setattr(scheduler, "_scheduler", fake_scheduler)

        def run_3h(*a, **k):
            wall["now"] = t0 + timedelta(hours=3)
            return []

        with caplog.at_level(logging.INFO, logger="sync_scheduler"):
            await _run_cycle(run_3h, _sync_service_cls(needs_sync=True))

        msg = _messages(caplog, "Cycle summary")[0].getMessage()
        assert "overdue by 1h00m" in msg


class TestSyncLawyerCasesPhases:
    @pytest.mark.asyncio
    async def test_listing_login_and_detail_time_are_charged_to_their_buckets(self, clock):
        mock_db = MagicMock()
        store = MagicMock()
        store.get_session_by_lawyer = AsyncMock(return_value=None)

        async def slow_reauth(lawyer, store_):
            clock.advance(11)
            return MagicMock(), None

        async def slow_listing(**kwargs):
            clock.advance(22)
            return []

        async def slow_detect(**kwargs):
            clock.advance(33)
            return 0, 0, []

        scraper = MagicMock()
        scraper.get_my_cases = AsyncMock(side_effect=slow_listing)
        scraper.close = AsyncMock()
        sync_cls = MagicMock()
        sync_cls.return_value.sync_cases.return_value = MagicMock(cases_total=0, cases_new=0)

        timer = cycle_timing.CycleTimer()
        with patch.object(scheduler, "get_session_store", return_value=store), \
             patch("app.api.v1.pjud.get_scraper", return_value=scraper), \
             patch.object(scheduler, "_reauth", side_effect=slow_reauth), \
             patch.object(scheduler, "SyncService", sync_cls), \
             patch.object(scheduler, "_select_cases_for_detail_rotation", return_value=[]), \
             patch.object(scheduler, "detect_and_sync_movements", side_effect=slow_detect), \
             cycle_timing.use_timer(timer):
            await scheduler.sync_lawyer_cases(1, "civil", mock_db)

        assert timer.totals["login"] == pytest.approx(11)
        assert timer.totals["listing"] == pytest.approx(22)
        assert timer.totals["detail"] == pytest.approx(33)

    @pytest.mark.asyncio
    async def test_detail_batch_receives_the_batch_size_cap(self):
        """The cap is what lets the closing line tell batch_size from no_more_cases."""
        mock_db = MagicMock()
        store = MagicMock()
        store.get_session_by_lawyer = AsyncMock(return_value=MagicMock())
        scraper = MagicMock()
        scraper.get_my_cases = AsyncMock(return_value=[])
        scraper.close = AsyncMock()
        sync_cls = MagicMock()
        sync_cls.return_value.sync_cases.return_value = MagicMock(cases_total=0, cases_new=0)
        detect = AsyncMock(return_value=(0, 0, []))

        with patch.object(scheduler, "get_session_store", return_value=store), \
             patch("app.api.v1.pjud.get_scraper", return_value=scraper), \
             patch.object(scheduler, "SyncService", sync_cls), \
             patch.object(scheduler, "_select_cases_for_detail_rotation", return_value=[]), \
             patch.object(scheduler, "detect_and_sync_movements", detect), \
             patch.object(scheduler.settings, "DETAIL_BATCH_SIZE", 17):
            await scheduler.sync_lawyer_cases(1, "civil", mock_db)

        assert detect.await_args.kwargs["batch_size_cap"] == 17
