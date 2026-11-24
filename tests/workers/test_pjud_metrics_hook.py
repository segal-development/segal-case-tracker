"""Scheduler: counters are flushed at every lawyer-unit boundary and at cycle end."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import pjud_metrics
from app.workers import sync_scheduler as ss


def test_flush_hook_uses_given_session_and_never_raises():
    db = MagicMock()
    with patch.object(pjud_metrics, "flush") as f:
        ss._flush_pjud_metrics(db)
    f.assert_called_once_with(db)
    with patch.object(pjud_metrics, "flush", side_effect=RuntimeError("x")):
        ss._flush_pjud_metrics(db)  # must not raise


@pytest.mark.asyncio
async def test_unit_and_cycle_end_flush_with_lawyer_context():
    seen = []
    with patch.object(ss, "_flush_pjud_metrics", lambda db: seen.append(db)), \
         patch.object(ss, "_wait_for_allowed_egress", AsyncMock()), \
         patch.object(ss, "_maybe_run_sysgal_incorporacion", AsyncMock()), \
         patch.object(ss, "_maybe_reiniciar_estacion", AsyncMock()), \
         patch.object(ss, "_active_lawyer_ids_by_staleness", return_value=[11]), \
         patch.object(ss, "SessionLocal", lambda: MagicMock(name="db")), \
         patch.object(ss, "_metrics_session_factory", lambda: MagicMock(name="mdb")), \
         patch.object(ss, "SyncService") as svc, \
         patch.object(ss, "sync_lawyer_cases", AsyncMock(side_effect=lambda *a: (seen.append(("ctx", pjud_metrics._current_lawyer.get())) or {"success": True}))), \
         patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", True), \
         patch("app.workers.sync_scheduler.asyncio.sleep", AsyncMock()):
        svc.return_value.needs_sync.return_value = True
        timer = MagicMock()
        timer.started_at = 0
        timer.totals = {}
        await ss._run_sync_cycle(timer)
    ctx = [x for x in seen if isinstance(x, tuple)]
    assert ctx and all(v == 11 for _, v in ctx)
    flushes = [x for x in seen if not isinstance(x, tuple)]
    assert len(flushes) >= len(ss.COMPETENCIAS) + 1  # every unit + cycle end
    assert pjud_metrics._current_lawyer.get() is None  # context restored


@pytest.mark.asyncio
async def test_failed_unit_pending_state_is_not_committed_by_the_metrics_flush():
    work_db = MagicMock(name="work_db")
    metrics_db = MagicMock(name="metrics_db")
    sessions = iter([MagicMock(name="setup_db"), work_db])
    flushed = []
    with patch.object(pjud_metrics, "flush", lambda db: flushed.append(db)), \
         patch.object(ss, "_wait_for_allowed_egress", AsyncMock()), \
         patch.object(ss, "_maybe_run_sysgal_incorporacion", AsyncMock()), \
         patch.object(ss, "_maybe_reiniciar_estacion", AsyncMock()), \
         patch.object(ss, "_active_lawyer_ids_by_staleness", return_value=[11]), \
         patch.object(ss, "SessionLocal", lambda: next(sessions)), \
         patch.object(ss, "_metrics_session_factory", lambda: metrics_db), \
         patch.object(ss, "SyncService") as svc, \
         patch.object(ss, "sync_lawyer_cases", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(ss, "COMPETENCIAS", ["civil"]), \
         patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", True), \
         patch("app.workers.sync_scheduler.asyncio.sleep", AsyncMock()):
        svc.return_value.needs_sync.return_value = True
        timer = MagicMock()
        timer.started_at = 0
        timer.totals = {}
        await ss._run_sync_cycle(timer)
    assert flushed and all(d is not work_db for d in flushed)
    work_db.commit.assert_not_called()
    work_db.rollback.assert_not_called()
    work_db.close.assert_called_once()
