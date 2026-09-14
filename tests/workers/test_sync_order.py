"""Sync cycle order: oldest successful sync first, never-synced first of all.

Found on 2026-09-14: the cycle iterated active lawyers in Postgres heap order
(no ORDER BY). With ~1h per lawyer and a worker that restarts daily, the same
6–7 lawyers synced every day while the rest — Carla among them — never got a
turn for two weeks. Ordering by staleness makes the rotation fair regardless of
where a cycle is interrupted.
"""

from datetime import datetime, timedelta

from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.workers.sync_scheduler import _active_lawyer_ids_by_staleness


def _lawyer(db, rut, *, active=True):
    l = Lawyer(rut=rut, name=f"L {rut}", role="lawyer", is_active=active)
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


def _sync(db, lawyer, *, ago_hours, status="completed"):
    at = datetime.utcnow() - timedelta(hours=ago_hours)
    db.add(SyncHistory(lawyer_id=lawyer.id, competencia="civil", started_at=at - timedelta(minutes=30),
                       completed_at=at if status == "completed" else None, status=status))
    db.commit()


class TestActiveLawyerIdsByStaleness:
    def test_never_synced_first_then_oldest_completed(self, db):
        recent = _lawyer(db, "10000000-1")
        stale = _lawyer(db, "20000000-2")
        never = _lawyer(db, "30000000-3")
        _sync(db, recent, ago_hours=1)
        _sync(db, stale, ago_hours=72)
        assert _active_lawyer_ids_by_staleness(db) == [never.id, stale.id, recent.id]

    def test_failed_attempts_do_not_count_as_a_sync(self, db):
        """Only COMPLETED syncs count: a lawyer whose recent attempts all failed is
        as stale as their last success (or never-synced)."""
        only_failed = _lawyer(db, "10000000-1")
        old_ok_recent_fail = _lawyer(db, "20000000-2")
        fresh = _lawyer(db, "30000000-3")
        _sync(db, only_failed, ago_hours=1, status="failed")
        _sync(db, old_ok_recent_fail, ago_hours=96)
        _sync(db, old_ok_recent_fail, ago_hours=2, status="failed")
        _sync(db, fresh, ago_hours=1)
        assert _active_lawyer_ids_by_staleness(db) == [only_failed.id, old_ok_recent_fail.id, fresh.id]

    def test_inactive_lawyers_are_excluded_and_ties_break_by_id(self, db):
        a = _lawyer(db, "10000000-1")
        b = _lawyer(db, "20000000-2")
        gone = _lawyer(db, "30000000-3", active=False)
        assert _active_lawyer_ids_by_staleness(db) == [a.id, b.id]
        assert gone.id not in _active_lawyer_ids_by_staleness(db)
