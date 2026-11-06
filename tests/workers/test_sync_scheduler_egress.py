"""Scheduler egress guard: no PJUD login for a lawyer while the egress IP is forbidden."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.workers.sync_scheduler as scheduler

OK = {"success": True, "cases_total": 1, "cases_new": 0}
OFFICE = "186.10.251.189"


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(scheduler, "_previous_cycle", None)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_auth_block_until", None, raising=False)


async def _run_cycle(lawyer_ids, verdicts):
    """Run one cycle; ``verdicts`` are the successive egress_allowed() answers."""
    queue = list(verdicts)
    order: list = []

    def egress():
        v = queue.pop(0) if len(queue) > 1 else queue[0]
        order.append("check")
        return v

    async def sync_impl(*_a, **_k):
        order.append("sync")
        return OK

    async def fake_sleep(seconds):
        order.append(("sleep", seconds))

    sync_mock = AsyncMock(side_effect=sync_impl)
    sync_cls = MagicMock()
    sync_cls.return_value.needs_sync.return_value = True
    egress_mock = MagicMock(side_effect=egress)
    with patch.object(scheduler, "SessionLocal", side_effect=lambda: MagicMock()), \
         patch.object(scheduler, "_active_lawyer_ids_by_staleness", return_value=lawyer_ids), \
         patch.object(scheduler, "SyncService", sync_cls), \
         patch.object(scheduler, "COMPETENCIAS", ["civil"]), \
         patch.object(scheduler, "MAX_DATA_AGE_HOURS", 4), \
         patch("app.services.credential_audit.scan_credential_changes", return_value=0), \
         patch("app.services.sysgal_sync.sync_sysgal_estados", MagicMock(return_value={})), \
         patch("app.services.scraping_health.revisar_y_avisar",
               return_value=MagicMock(requiere_aviso=False)), \
         patch.object(scheduler, "_maybe_take_cartera_snapshot"), \
         patch.object(scheduler, "_run_semaforo_alerts_safely"), \
         patch.object(scheduler, "egress_allowed", egress_mock), \
         patch.object(scheduler.asyncio, "sleep", fake_sleep), \
         patch.object(scheduler, "sync_lawyer_cases", sync_mock):
        await scheduler.sync_all_lawyers()
    return sync_mock, order, egress_mock


@pytest.mark.asyncio
async def test_allowed_ip_proceeds_unchanged():
    sync_mock, order, _ = await _run_cycle([1, 2], [(True, "1.2.3.4", "allowed")])
    assert [c.args[0] for c in sync_mock.await_args_list] == [1, 2]
    assert not any(isinstance(o, tuple) and o[1] >= 300 for o in order)


@pytest.mark.asyncio
async def test_forbidden_ip_makes_no_pjud_call_waits_then_rechecks():
    verdicts = [
        (False, OFFICE, "forbidden"),
        (False, None, "unknown"),
        (True, "1.2.3.4", "allowed"),
    ]
    sync_mock, order, egress_mock = await _run_cycle([1], verdicts)
    # No sync before the verdict flipped, waits of >= 5 min in between.
    assert order.index("sync") > 4
    waits = [o[1] for o in order[: order.index("sync")] if isinstance(o, tuple)]
    assert len(waits) == 2 and all(w >= 300 for w in waits)
    assert sync_mock.await_count == 1
    assert egress_mock.call_count == 3


@pytest.mark.asyncio
async def test_egress_stop_reason_marks_the_sync_partial():
    assert scheduler._batch_stop_reason([f"Red no permitida: {OFFICE}"]) == (
        f"Red no permitida: {OFFICE}"
    )


@pytest.mark.asyncio
async def test_guard_off_never_looks_up_nor_waits(monkeypatch):
    from app.config import settings
    from app.services import egress_guard

    monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", "")

    def no_lookup(*_a, **_k):
        raise AssertionError("IP lookup with the guard off")

    monkeypatch.setattr(egress_guard, "_default_fetchers", no_lookup)
    sleeps: list[float] = []

    async def spy_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(scheduler.asyncio, "sleep", spy_sleep)
    await scheduler._wait_for_allowed_egress()
    assert sleeps == []
